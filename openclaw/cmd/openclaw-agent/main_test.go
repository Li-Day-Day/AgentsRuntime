package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	appconfig "github.com/iamlovingit/clawmanager-openclaw-image/internal/config"
)

func TestNormalizeRuntimeConfigEnablesRedisTeamForDisabledLiteMode(t *testing.T) {
	root := t.TempDir()
	configPath := filepath.Join(root, "openclaw.json")
	extensionsDir := filepath.Join(root, "extensions")
	if err := os.MkdirAll(filepath.Join(extensionsDir, "redis-team"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(extensionsDir, "redis-team", "openclaw.plugin.json"), []byte(`{"channels":["redis-team"]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(configPath, []byte(`{
		"channels": {},
		"plugins": {
			"entries": {
				"redis-team": {"enabled": false}
			}
		}
	}`), 0o644); err != nil {
		t.Fatal(err)
	}

	t.Setenv("CLAWMANAGER_TEAM_ENABLED", "true")
	t.Setenv("CLAWMANAGER_LLM_MODEL", "")
	t.Setenv("CLAWMANAGER_LLM_BASE_URL", "")
	unsetEnvForTest(t, "CLAWMANAGER_LLM_API_KEY")
	unsetEnvForTest(t, "OPENAI_API_KEY")

	if err := normalizeRuntimeConfig(appconfig.Config{
		OpenClawConfigPath:    configPath,
		OpenClawExtensionsDir: extensionsDir,
	}); err != nil {
		t.Fatal(err)
	}

	data, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	var cfg map[string]any
	if err := json.Unmarshal(data, &cfg); err != nil {
		t.Fatal(err)
	}
	if got := nestedMapForTest(t, cfg, "plugins", "entries", "redis-team")["enabled"]; got != true {
		t.Fatalf("redis-team plugin enabled = %#v, want true", got)
	}
	if got := nestedMapForTest(t, cfg, "channels", "redis-team", "accounts", "default")["fromEnv"]; got != true {
		t.Fatalf("redis-team default account fromEnv = %#v, want true", got)
	}
}

func unsetEnvForTest(t *testing.T, key string) {
	t.Helper()
	previous, existed := os.LookupEnv(key)
	if err := os.Unsetenv(key); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if existed {
			_ = os.Setenv(key, previous)
			return
		}
		_ = os.Unsetenv(key)
	})
}

func nestedMapForTest(t *testing.T, root map[string]any, path ...string) map[string]any {
	t.Helper()
	current := root
	for _, part := range path {
		next, ok := current[part].(map[string]any)
		if !ok {
			t.Fatalf("expected object at %v, got %#v", path, current[part])
		}
		current = next
	}
	return current
}
