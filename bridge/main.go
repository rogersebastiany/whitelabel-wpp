package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"github.com/mdp/qrterminal/v3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

var (
	client  *whatsmeow.Client
	sentIDs sync.Map // track message IDs we sent, to ignore echoes
)

// WebhookPayload is what we POST to the Python app on every incoming message.
type WebhookPayload struct {
	Action       string `json:"action"`
	SenderPhone  string `json:"sender_phone"`
	SenderName   string `json:"sender_name"`
	GroupID      string `json:"group_id"`
	GroupName    string `json:"group_name"`
	MsgID        string `json:"msg_id"`
	Timestamp    string `json:"timestamp"`
	Text         string `json:"text"`
	ReplyToMsgID string `json:"reply_to_msg_id"`
	HasMedia     bool   `json:"has_media"`
	MediaType    string `json:"media_type"`
	OwnerQuery   bool   `json:"owner_query"`
}

// SendRequest is what the Python app POSTs to us when it wants to send a message.
type SendRequest struct {
	To   string `json:"to"`
	Text string `json:"text"`
}

func main() {
	webhookURL := flag.String("webhook", "http://localhost:8000/webhook/whatsmeow", "Python app webhook URL")
	listenAddr := flag.String("listen", ":8080", "HTTP listen address for send API")
	sessionDB := flag.String("session", "session.db", "SQLite session database path")
	ownerPhone := flag.String("owner", "", "Owner phone number (for 1:1 chat detection)")
	flag.Parse()

	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	slog.Info("starting bridge", "webhook", *webhookURL, "listen", *listenAddr, "session", *sessionDB)

	ctx := context.Background()

	dbLog := waLog.Stdout("DB", "WARN", true)
	container, err := sqlstore.New(ctx, "sqlite3", "file:"+*sessionDB+"?_foreign_keys=on", dbLog)
	if err != nil {
		slog.Error("failed to create store", "error", err)
		os.Exit(1)
	}

	device, err := container.GetFirstDevice(ctx)
	if err != nil {
		slog.Error("failed to get device", "error", err)
		os.Exit(1)
	}

	clientLog := waLog.Stdout("Client", "WARN", true)
	client = whatsmeow.NewClient(device, clientLog)

	client.AddEventHandler(func(evt interface{}) {
		handleEvent(evt, *webhookURL, *ownerPhone)
	})

	if client.Store.ID == nil {
		slog.Info("no session found, generating QR code...")
		qrChan, _ := client.GetQRChannel(ctx)
		if err := client.Connect(); err != nil {
			slog.Error("failed to connect", "error", err)
			os.Exit(1)
		}
		for evt := range qrChan {
			switch evt.Event {
			case "code":
				fmt.Println()
				qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
				fmt.Println("\nScan with WhatsApp to link this device.")
			case "success":
				slog.Info("authenticated successfully")
			case "timeout":
				slog.Error("QR code timed out")
				os.Exit(1)
			}
		}
	} else {
		slog.Info("session found, connecting...", "jid", client.Store.ID.String())
		if err := client.Connect(); err != nil {
			slog.Error("failed to connect", "error", err)
			os.Exit(1)
		}
	}

	slog.Info("connected, waiting for messages...")

	// HTTP server for the Python app to send messages through
	mux := http.NewServeMux()
	mux.HandleFunc("POST /send", handleSend)
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		connected := client.IsConnected()
		status := "disconnected"
		if connected {
			status = "connected"
		}
		json.NewEncoder(w).Encode(map[string]interface{}{
			"status":    status,
			"connected": connected,
		})
	})

	server := &http.Server{Addr: *listenAddr, Handler: mux}
	go func() {
		slog.Info("send API listening", "addr", *listenAddr)
		if err := server.ListenAndServe(); err != http.ErrServerClosed {
			slog.Error("HTTP server error", "error", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, os.Interrupt, syscall.SIGTERM)
	<-quit

	slog.Info("shutting down...")
	server.Close()
	client.Disconnect()
}

func handleEvent(evt interface{}, webhookURL, ownerPhone string) {
	msg, ok := evt.(*events.Message)
	if !ok {
		return
	}

	// Ignore echoes of messages we sent
	if _, wasSent := sentIDs.LoadAndDelete(msg.Info.ID); wasSent {
		return
	}

	from := msg.Info.Sender.User
	if msg.Info.Sender.Server == "lid" {
		if pn, err := client.Store.LIDs.GetPNForLID(context.Background(), msg.Info.Sender); err == nil && !pn.IsEmpty() {
			from = pn.User
		}
	}

	// Extract text
	var text string
	if msg.Message.ExtendedTextMessage != nil {
		text = msg.Message.ExtendedTextMessage.GetText()
	} else {
		text = msg.Message.GetConversation()
	}

	hasMedia := false
	mediaType := ""
	if msg.Message.ImageMessage != nil {
		hasMedia = true
		mediaType = "image"
	} else if msg.Message.AudioMessage != nil {
		hasMedia = true
		mediaType = "audio"
	} else if msg.Message.VideoMessage != nil {
		hasMedia = true
		mediaType = "video"
	} else if msg.Message.DocumentMessage != nil {
		hasMedia = true
		mediaType = "document"
	} else if msg.Message.StickerMessage != nil {
		hasMedia = true
		mediaType = "sticker"
	}

	if text == "" && !hasMedia {
		return
	}

	chat := msg.Info.Chat
	isGroup := chat.Server == "g.us"

	// Extract reply context
	replyToMsgID := ""
	if msg.Message.ExtendedTextMessage != nil && msg.Message.ExtendedTextMessage.ContextInfo != nil {
		if stanzaID := msg.Message.ExtendedTextMessage.ContextInfo.StanzaID; stanzaID != nil {
			replyToMsgID = *stanzaID
		}
	}

	groupID := ""
	groupName := ""
	ownerQuery := false

	if isGroup {
		groupID = chat.String()
		// Try to get group name from store
		if gi, err := client.GetGroupInfo(context.Background(), chat); err == nil {
			groupName = gi.Name
		}
	} else {
		// 1:1 chat — check if it's the owner
		if ownerPhone != "" && from == ownerPhone {
			ownerQuery = true
		}
	}

	payload := WebhookPayload{
		Action:       "message_received",
		SenderPhone:  from,
		SenderName:   msg.Info.PushName,
		GroupID:      groupID,
		GroupName:    groupName,
		MsgID:        msg.Info.ID,
		Timestamp:    fmt.Sprintf("%d", msg.Info.Timestamp.Unix()),
		Text:         text,
		ReplyToMsgID: replyToMsgID,
		HasMedia:     hasMedia,
		MediaType:    mediaType,
		OwnerQuery:   ownerQuery,
	}

	slog.Info("message received",
		"from", from,
		"group", groupID,
		"text_len", len(text),
		"is_group", isGroup,
		"owner_query", ownerQuery,
		"reply_to", replyToMsgID,
	)

	go postWebhook(webhookURL, payload)
}

func postWebhook(url string, payload WebhookPayload) {
	body, err := json.Marshal(payload)
	if err != nil {
		slog.Error("failed to marshal webhook payload", "error", err)
		return
	}

	resp, err := http.Post(url, "application/json", bytes.NewReader(body))
	if err != nil {
		slog.Error("webhook POST failed", "error", err, "url", url)
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		slog.Error("webhook returned error", "status", resp.StatusCode, "url", url)
	}
}

func handleSend(w http.ResponseWriter, r *http.Request) {
	var req SendRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}

	if req.To == "" || req.Text == "" {
		http.Error(w, "to and text are required", http.StatusBadRequest)
		return
	}

	recipient := parseRecipient(req.To)
	if recipient.IsEmpty() {
		http.Error(w, "invalid recipient", http.StatusBadRequest)
		return
	}

	resp, err := client.SendMessage(context.Background(), recipient, &waE2E.Message{
		Conversation: proto.String(req.Text),
	})
	if err != nil {
		slog.Error("send failed", "error", err, "to", req.To)
		http.Error(w, "send failed: "+err.Error(), http.StatusInternalServerError)
		return
	}

	sentIDs.Store(resp.ID, true)
	slog.Info("sent message", "to", req.To, "id", resp.ID, "ts", resp.Timestamp.Format(time.RFC3339))

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{
		"status": "sent",
		"id":     resp.ID,
	})
}

func parseRecipient(to string) types.JID {
	jid := to
	if !strings.Contains(jid, "@") {
		jid += "@s.whatsapp.net"
	}
	recipient, err := types.ParseJID(jid)
	if err != nil {
		slog.Error("invalid JID", "error", err, "to", to)
		return types.JID{}
	}
	return recipient
}
