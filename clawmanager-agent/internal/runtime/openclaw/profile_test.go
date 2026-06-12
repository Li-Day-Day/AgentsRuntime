package openclaw_test

import (
	"testing"

	"github.com/iamlovingit/clawmanager-agent/internal/runtime/openclaw"
)

func TestProfileSupportsOpenClawRuntimeTypes(t *testing.T) {
	for _, runtimeType := range []string{"openclaw", "openclaw-shell"} {
		profile := openclaw.NewProfile(runtimeType)
		if profile.Type() != runtimeType {
			t.Fatalf("Type() = %q, want %q", profile.Type(), runtimeType)
		}
		if profile.Defaults().GatewayPortBlockSize != 3 {
			t.Fatalf("GatewayPortBlockSize = %d, want 3", profile.Defaults().GatewayPortBlockSize)
		}
	}
}
