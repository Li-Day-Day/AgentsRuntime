package instanceagent

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestAPIClientRegisterAndHeartbeat(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agent/register", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Fatalf("unexpected register method: %s", r.Method)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer bootstrap-token" {
			t.Fatalf("unexpected register auth: %s", got)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body["agent_id"] != "hermes-123-main" {
			t.Fatalf("unexpected agent_id: %v", body["agent_id"])
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"data": map[string]any{
				"session_token":                 "session-token",
				"heartbeat_interval_seconds":    7,
				"command_poll_interval_seconds": 3,
			},
		})
	})
	mux.HandleFunc("/api/v1/agent/heartbeat", func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer session-token" {
			t.Fatalf("unexpected heartbeat auth: %s", got)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"data": map[string]any{"has_pending_command": true},
		})
	})
	server := httptest.NewServer(mux)
	defer server.Close()

	cfg := Config{
		BaseURL:         server.URL,
		BootstrapToken:  "bootstrap-token",
		InstanceID:      "123",
		AgentID:         "hermes-123-main",
		AgentVersion:    "test",
		ProtocolVersion: "v1",
		PersistentDir:   "/config",
	}
	client := newAPIClient(cfg)

	reg, err := client.register(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if reg.SessionToken != "session-token" || reg.HeartbeatIntervalSeconds != 7 || reg.CommandPollIntervalSeconds != 3 {
		t.Fatalf("unexpected register response: %+v", reg)
	}

	hb, err := client.heartbeat(context.Background(), reg.SessionToken, HeartbeatBody{
		AgentID:        cfg.AgentID,
		Timestamp:      time.Now().UTC(),
		OpenClawStatus: "running",
		Summary:        map[string]any{"runtime": "hermes"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !hb.HasPendingCommand {
		t.Fatal("expected pending command flag")
	}
}
