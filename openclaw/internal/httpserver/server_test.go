package httpserver

import (
	"strings"
	"testing"

	"github.com/iamlovingit/clawmanager-openclaw-image/internal/process"
)

func TestOpenClawWaitReadyDoesNotRequireGatewayWarmup(t *testing.T) {
	if !openClawWaitReady(process.Snapshot{Status: process.StatusRunning, GatewayWarmupReady: false}) {
		t.Fatal("running gateway should release wait page even while models warmup continues")
	}
}

func TestOpenClawWaitReadyDoesNotReleaseStartingGateway(t *testing.T) {
	if openClawWaitReady(process.Snapshot{Status: process.StatusStarting}) {
		t.Fatal("starting gateway should not release wait page before gateway readiness promotion")
	}
}

func TestOpenClawWaitReadyReleasesWhenWarmupStarted(t *testing.T) {
	if !openClawWaitReady(process.Snapshot{Status: process.StatusStarting, GatewayWarmupStarted: true}) {
		t.Fatal("started warmup should release wait page into bounded warmup wait")
	}
}

func TestOpenClawWaitPageStartsWarmupTimeoutAfterGatewayReady(t *testing.T) {
	page := openClawWaitPage("http://localhost:18789")
	if !strings.Contains(page, "let gatewayReadyAt = 0") {
		t.Fatal("wait page should track when the gateway first becomes ready")
	}
	if !strings.Contains(page, "gatewayReadyAt = Date.now()") {
		t.Fatal("wait page should start warmup timeout after gateway readiness")
	}
	if strings.Contains(page, "const startedAt = Date.now()") {
		t.Fatal("wait page should not start warmup timeout at page load")
	}
}

func TestOpenClawWaitPageUsesServerSideGatewayToken(t *testing.T) {
	t.Setenv("OPENCLAW_GATEWAY_TOKEN", "token /?+&=")

	page := openClawWaitPage("https://untrusted.example.invalid/")
	wantTarget := `const target = "http://localhost:18789/#token=token+%2F%3F%2B%26%3D";`
	if !strings.Contains(page, wantTarget) {
		t.Fatal("wait page target does not contain URL-encoded server token")
	}
	if strings.Contains(page, "untrusted.example.invalid") {
		t.Fatal("wait page must not attach the gateway token to a caller-provided target")
	}
	if strings.Contains(page, "/openclaw-wait?target=") {
		t.Fatal("wait page must not put the gateway token in a target query parameter")
	}
}

func TestOpenClawWaitPageWithoutGatewayTokenKeepsOriginalTarget(t *testing.T) {
	t.Setenv("OPENCLAW_GATEWAY_TOKEN", "")

	page := openClawWaitPage("http://localhost:18789/workspace")
	if !strings.Contains(page, `const target = "http://localhost:18789/workspace";`) {
		t.Fatal("wait page should keep the original target when no gateway token is configured")
	}
	if strings.Contains(page, "#token=") {
		t.Fatal("wait page should not add a token fragment when no gateway token is configured")
	}
}
