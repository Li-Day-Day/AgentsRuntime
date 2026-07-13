package config

import (
	"path/filepath"
	"reflect"
	"testing"
)

func TestLoadDefaultsToWrappedChromium(t *testing.T) {
	t.Setenv("OPENCLAW_AGENT_CONFIG_PATH", filepath.Join(t.TempDir(), "missing.yaml"))
	t.Setenv("OPENCLAW_AGENT_INSTANCE_ID", "test-instance")
	t.Setenv("OPENCLAW_AGENT_BOOTSTRAP_TOKEN", "test-token")
	t.Setenv("OPENCLAW_AGENT_CONTROL_PLANE_BASE_URL", "http://control-plane.test")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.BrowserExecutable != "/usr/local/bin/wrapped-chromium" {
		t.Fatalf("BrowserExecutable = %q, want wrapped chromium", cfg.BrowserExecutable)
	}
	wantCommand := []string{"openclaw", "gateway", "run", "--auth", "token"}
	if !reflect.DeepEqual(cfg.OpenClawCommand, wantCommand) {
		t.Fatalf("OpenClawCommand = %#v, want %#v", cfg.OpenClawCommand, wantCommand)
	}
}
