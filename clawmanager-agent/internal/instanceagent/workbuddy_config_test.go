package instanceagent

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestLoadConfigUsesWorkBuddyPaths(t *testing.T) {
	t.Setenv("CLAWMANAGER_AGENT_RUNTIME_TYPE", "workbuddy")
	t.Setenv("CLAWMANAGER_RUNTIME_TYPE", "desktop")
	t.Setenv("CLAWMANAGER_AGENT_PERSISTENT_DIR", "/config")
	t.Setenv("CLAWMANAGER_AGENT_SKILL_DIRS", "")
	t.Setenv("HERMES_SKILL_DIRS", "")
	t.Setenv("WORKBUDDY_MODEL_CONFIG_PATH", "")

	cfg, err := LoadConfig("test")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.RuntimeType != "workbuddy" {
		t.Fatalf("RuntimeType = %q, want workbuddy", cfg.RuntimeType)
	}
	if cfg.AgentID != "workbuddy-unknown-main" {
		t.Fatalf("AgentID = %q", cfg.AgentID)
	}
	if cfg.SkillInstallDir != "/config/.workbuddy/skills" {
		t.Fatalf("SkillInstallDir = %q", cfg.SkillInstallDir)
	}
	if cfg.ModelConfigPath != "/config/.workbuddy/models.json" {
		t.Fatalf("ModelConfigPath = %q", cfg.ModelConfigPath)
	}
	if cfg.RuntimeVersionFile != "/opt/workbuddy/.workbuddy-linux/build-info.json" {
		t.Fatalf("RuntimeVersionFile = %q", cfg.RuntimeVersionFile)
	}
	if cfg.WorkDir() != filepath.Join(string(filepath.Separator), "config", ".workbuddy", "agent") {
		t.Fatalf("WorkDir() = %q", cfg.WorkDir())
	}
}

func TestApplyManagedRuntimeConfigWritesWorkBuddyCompatibilityPath(t *testing.T) {
	clearWorkBuddyModelEnv(t)
	root := t.TempDir()
	t.Setenv("CLAWMANAGER_LLM_MODEL", `["custom-model"]`)
	t.Setenv("CLAWMANAGER_LLM_BASE_URL", "https://gateway.example/v1")
	t.Setenv("CLAWMANAGER_LLM_API_KEY", "gateway-key")
	primary := filepath.Join(root, ".workbuddy", "models.json")

	if _, err := ApplyManagedRuntimeConfig(Config{RuntimeType: "workbuddy", ModelConfigPath: primary}); err != nil {
		t.Fatal(err)
	}
	primaryData, err := os.ReadFile(primary)
	if err != nil {
		t.Fatal(err)
	}
	legacyData, err := os.ReadFile(filepath.Join(root, ".workbuddy", "model.json"))
	if err != nil {
		t.Fatal(err)
	}
	if string(primaryData) != string(legacyData) {
		t.Fatal("models.json and model.json differ")
	}
}

func TestApplyManagedRuntimeConfigWritesExplicitModels(t *testing.T) {
	clearWorkBuddyModelEnv(t)
	modelPath := filepath.Join(t.TempDir(), ".workbuddy", "model.json")
	t.Setenv("CLAWMANAGER_WORKBUDDY_MODELS_JSON", `[
  {"id":"deepseek-v4-flash","name":"deepseek-v4-flash","vendor":"Custom","url":"https://gateway.example/v1","apiKey":"secret-one","supportsToolCall":true,"supportsImages":false,"supportsReasoning":false,"useCustomProtocol":false},
  {"id":"kimi-k2.6","name":"kimi-k2.6","vendor":"Custom","url":"https://gateway.example/v1","apiKey":"secret-two","supportsToolCall":true,"supportsImages":false,"supportsReasoning":false,"useCustomProtocol":false}
]`)

	result, err := ApplyManagedRuntimeConfig(Config{RuntimeType: "workbuddy", ModelConfigPath: modelPath})
	if err != nil {
		t.Fatal(err)
	}
	if !result.Changed || result.ModelCount != 2 || result.Digest == "" {
		t.Fatalf("result = %+v", result)
	}
	data, err := os.ReadFile(modelPath)
	if err != nil {
		t.Fatal(err)
	}
	var models []WorkBuddyModel
	if err := json.Unmarshal(data, &models); err != nil {
		t.Fatalf("model.json is not strict JSON: %v\n%s", err, data)
	}
	if models[0].ID != "deepseek-v4-flash" || models[1].APIKey != "secret-two" {
		t.Fatalf("models = %+v", models)
	}
	info, err := os.Stat(modelPath)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 {
		t.Fatalf("model.json mode = %o, want 600", info.Mode().Perm())
	}

	second, err := ApplyManagedRuntimeConfig(Config{RuntimeType: "workbuddy", ModelConfigPath: modelPath})
	if err != nil {
		t.Fatal(err)
	}
	if second.Changed {
		t.Fatal("unchanged model config was rewritten")
	}
}

func TestApplyManagedRuntimeConfigBuildsModelsFromGatewayEnv(t *testing.T) {
	clearWorkBuddyModelEnv(t)
	modelPath := filepath.Join(t.TempDir(), "model.json")
	t.Setenv("CLAWMANAGER_LLM_MODEL", `["deepseek-v4-flash","kimi-k2.6"]`)
	t.Setenv("CLAWMANAGER_LLM_BASE_URL", "https://gateway.example/v1")
	t.Setenv("CLAWMANAGER_LLM_API_KEY", "gateway-key")

	result, err := ApplyManagedRuntimeConfig(Config{RuntimeType: "workbuddy", ModelConfigPath: modelPath})
	if err != nil {
		t.Fatal(err)
	}
	if result.ModelCount != 2 {
		t.Fatalf("ModelCount = %d", result.ModelCount)
	}
	data, _ := os.ReadFile(modelPath)
	var models []WorkBuddyModel
	if err := json.Unmarshal(data, &models); err != nil {
		t.Fatal(err)
	}
	for _, model := range models {
		if model.URL != "https://gateway.example/v1" || model.APIKey != "gateway-key" || !model.SupportsToolCall {
			t.Fatalf("model = %+v", model)
		}
	}
}

func TestApplyManagedRuntimeConfigDoesNotEraseExistingWithoutInjection(t *testing.T) {
	clearWorkBuddyModelEnv(t)
	modelPath := filepath.Join(t.TempDir(), "model.json")
	if err := os.WriteFile(modelPath, []byte("user-owned"), 0o600); err != nil {
		t.Fatal(err)
	}
	result, err := ApplyManagedRuntimeConfig(Config{RuntimeType: "workbuddy", ModelConfigPath: modelPath})
	if err != nil {
		t.Fatal(err)
	}
	if result.Changed {
		t.Fatal("config changed without injected model environment")
	}
	data, _ := os.ReadFile(modelPath)
	if string(data) != "user-owned" {
		t.Fatalf("model config = %q", data)
	}
}

func TestExplicitModelsRejectTrailingComma(t *testing.T) {
	clearWorkBuddyModelEnv(t)
	t.Setenv("CLAWMANAGER_WORKBUDDY_MODELS_JSON", `[{"id":"a","url":"https://example/v1",}]`)
	_, err := ApplyManagedRuntimeConfig(Config{RuntimeType: "workbuddy", ModelConfigPath: filepath.Join(t.TempDir(), "model.json")})
	if err == nil {
		t.Fatal("trailing comma was accepted")
	}
}

func clearWorkBuddyModelEnv(t *testing.T) {
	t.Helper()
	for _, key := range []string{
		"CLAWMANAGER_WORKBUDDY_MODELS_JSON", "WORKBUDDY_MODELS_JSON", "CLAWMANAGER_LLM_MODELS_JSON",
		"CLAWMANAGER_LLM_MODEL", "CLAWMANAGER_LLM_BASE_URL", "CLAWMANAGER_LLM_API_KEY",
		"OPENAI_MODEL", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_API_KEY",
	} {
		t.Setenv(key, "")
	}
}
