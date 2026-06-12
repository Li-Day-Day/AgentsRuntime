package instanceagent

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"time"
)

func (a *Agent) runLocalServer(ctx context.Context) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"status": "ok",
			"agent":  a.Snapshot(),
		})
	})
	mux.HandleFunc("/state", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, a.Snapshot())
	})
	mux.HandleFunc("/skills", func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		skills := append([]SkillInfo(nil), a.lastInventory...)
		a.mu.Unlock()
		writeJSON(w, http.StatusOK, map[string]any{"skills": skills})
	})
	mux.HandleFunc("/commands/poll", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		go a.processNextCommand(context.WithoutCancel(ctx))
		writeJSON(w, http.StatusAccepted, map[string]any{"accepted": true})
	})

	server := &http.Server{
		Addr:              a.cfg.HTTPAddr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownCtx)
	}()

	a.logger.Info("local instance-agent health API listening", "addr", a.cfg.HTTPAddr)
	err := server.ListenAndServe()
	if errors.Is(err, http.ErrServerClosed) {
		return context.Canceled
	}
	return err
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
