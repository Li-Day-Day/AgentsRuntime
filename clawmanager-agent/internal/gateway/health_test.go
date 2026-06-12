package gateway

import (
	"context"
	"net"
	"net/http"
	"testing"
	"time"
)

func TestHTTPGatewayHealthCheckerUsesNonUpgradingOriginProbe(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer listener.Close()

	upgradeSeen := false
	server := &http.Server{
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("Upgrade") != "" {
				upgradeSeen = true
			}
			w.WriteHeader(http.StatusOK)
		}),
	}
	go func() {
		_ = server.Serve(listener)
	}()
	defer server.Shutdown(context.Background())

	port := listener.Addr().(*net.TCPAddr).Port
	checker := NewHTTPGatewayHealthChecker(Config{
		PublicOrigin:          "http://clawmanager-gateway.clawmanager-system.svc.cluster.local:9001",
		GatewayStartupTimeout: time.Second,
	})

	if err := checker.WaitReady(context.Background(), GatewayStartSpec{Port: port}); err != nil {
		t.Fatalf("WaitReady() error = %v", err)
	}
	if upgradeSeen {
		t.Fatal("WaitReady() sent a websocket upgrade probe, want ordinary HTTP origin check only")
	}
}
