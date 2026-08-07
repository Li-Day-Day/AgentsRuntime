package instanceagent

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// WorkBuddyModel is the on-disk schema consumed by WorkBuddy Linux.
type WorkBuddyModel struct {
	ID                string `json:"id"`
	Name              string `json:"name"`
	Vendor            string `json:"vendor"`
	URL               string `json:"url"`
	APIKey            string `json:"apiKey"`
	SupportsToolCall  bool   `json:"supportsToolCall"`
	SupportsImages    bool   `json:"supportsImages"`
	SupportsReasoning bool   `json:"supportsReasoning"`
	UseCustomProtocol bool   `json:"useCustomProtocol"`
}

type ManagedRuntimeConfigResult struct {
	Changed    bool
	ModelCount int
	Digest     string
}

// ApplyManagedRuntimeConfig applies product-specific configuration supplied by
// ClawManager. It intentionally does nothing for WorkBuddy when no model input
// is present, so a user's existing model.json is never erased accidentally.
func ApplyManagedRuntimeConfig(cfg Config) (ManagedRuntimeConfigResult, error) {
	if cfg.RuntimeType != "workbuddy" {
		return ManagedRuntimeConfigResult{}, nil
	}
	models, configured, err := workBuddyModelsFromEnv()
	if err != nil {
		return ManagedRuntimeConfigResult{}, err
	}
	if !configured {
		return ManagedRuntimeConfigResult{}, nil
	}
	if strings.TrimSpace(cfg.ModelConfigPath) == "" || !filepath.IsAbs(cfg.ModelConfigPath) {
		return ManagedRuntimeConfigResult{}, fmt.Errorf("WORKBUDDY_MODEL_CONFIG_PATH must be absolute: %s", cfg.ModelConfigPath)
	}

	payload, err := json.MarshalIndent(models, "", "  ")
	if err != nil {
		return ManagedRuntimeConfigResult{}, fmt.Errorf("marshal WorkBuddy models: %w", err)
	}
	payload = append(payload, '\n')
	sum := sha256.Sum256(payload)
	result := ManagedRuntimeConfigResult{ModelCount: len(models), Digest: hex.EncodeToString(sum[:])}

	paths := []string{cfg.ModelConfigPath}
	if filepath.Base(cfg.ModelConfigPath) == "models.json" {
		paths = append(paths, filepath.Join(filepath.Dir(cfg.ModelConfigPath), "model.json"))
	}
	allCurrent := true
	for _, configPath := range paths {
		existing, readErr := os.ReadFile(configPath)
		if readErr != nil || !bytes.Equal(existing, payload) {
			allCurrent = false
			break
		}
	}
	if allCurrent {
		return result, nil
	}
	for _, configPath := range paths {
		if err := writeFileAtomic(configPath, payload, 0o600); err != nil {
			return ManagedRuntimeConfigResult{}, fmt.Errorf("write WorkBuddy model config %s: %w", configPath, err)
		}
	}
	result.Changed = true
	return result, nil
}

func workBuddyModelsFromEnv() ([]WorkBuddyModel, bool, error) {
	for _, key := range []string{
		"CLAWMANAGER_WORKBUDDY_MODELS_JSON",
		"WORKBUDDY_MODELS_JSON",
		"CLAWMANAGER_LLM_MODELS_JSON",
	} {
		if raw, ok := os.LookupEnv(key); ok && strings.TrimSpace(raw) != "" {
			models, err := parseWorkBuddyModelsJSON(raw)
			if err != nil {
				return nil, true, fmt.Errorf("parse %s: %w", key, err)
			}
			return models, true, nil
		}
	}

	rawModels := strings.TrimSpace(os.Getenv("CLAWMANAGER_LLM_MODEL"))
	if strings.HasPrefix(rawModels, "[") {
		var probe []json.RawMessage
		if err := json.Unmarshal([]byte(rawModels), &probe); err == nil && len(probe) > 0 && strings.HasPrefix(strings.TrimSpace(string(probe[0])), "{") {
			models, parseErr := parseWorkBuddyModelsJSON(rawModels)
			if parseErr != nil {
				return nil, true, fmt.Errorf("parse CLAWMANAGER_LLM_MODEL model objects: %w", parseErr)
			}
			return models, true, nil
		}
	}

	baseURL := firstNonEmptyEnv("CLAWMANAGER_LLM_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE")
	if rawModels == "" {
		rawModels = strings.TrimSpace(os.Getenv("OPENAI_MODEL"))
	}
	if rawModels == "" {
		return nil, false, nil
	}
	apiKey, _ := lookupFirstEnv("CLAWMANAGER_LLM_API_KEY", "OPENAI_API_KEY")
	modelIDs, err := parseModelIDs(rawModels)
	if err != nil {
		return nil, true, fmt.Errorf("parse CLAWMANAGER_LLM_MODEL: %w", err)
	}
	if len(modelIDs) == 0 {
		return nil, true, errors.New("model injection requires at least one model id")
	}
	if baseURL == "" {
		return nil, true, errors.New("model injection requires CLAWMANAGER_LLM_BASE_URL or OPENAI_BASE_URL")
	}

	models := make([]WorkBuddyModel, 0, len(modelIDs))
	for _, id := range modelIDs {
		models = append(models, WorkBuddyModel{
			ID:               id,
			Name:             id,
			Vendor:           "Custom",
			URL:              baseURL,
			APIKey:           apiKey,
			SupportsToolCall: true,
		})
	}
	return models, true, nil
}

func parseWorkBuddyModelsJSON(raw string) ([]WorkBuddyModel, error) {
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.DisallowUnknownFields()
	var models []WorkBuddyModel
	if err := decoder.Decode(&models); err != nil {
		return nil, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		if err == nil {
			return nil, errors.New("unexpected JSON value after model array")
		}
		return nil, err
	}
	if len(models) == 0 {
		return nil, errors.New("model array must not be empty")
	}
	seen := make(map[string]bool, len(models))
	for index := range models {
		model := &models[index]
		model.ID = strings.TrimSpace(model.ID)
		model.Name = strings.TrimSpace(model.Name)
		model.Vendor = strings.TrimSpace(model.Vendor)
		model.URL = strings.TrimSpace(model.URL)
		if model.ID == "" {
			return nil, fmt.Errorf("model at index %d has an empty id", index)
		}
		if seen[model.ID] {
			return nil, fmt.Errorf("duplicate model id %q", model.ID)
		}
		seen[model.ID] = true
		if model.Name == "" {
			model.Name = model.ID
		}
		if model.Vendor == "" {
			model.Vendor = "Custom"
		}
		if model.URL == "" {
			return nil, fmt.Errorf("model %q has an empty url", model.ID)
		}
	}
	return models, nil
}

func parseModelIDs(raw string) ([]string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	var values []string
	if strings.HasPrefix(raw, "[") {
		if err := json.Unmarshal([]byte(raw), &values); err != nil {
			return nil, err
		}
	} else {
		values = []string{raw}
	}
	seen := map[string]bool{}
	result := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value != "" && !seen[value] {
			seen[value] = true
			result = append(result, value)
		}
	}
	return result, nil
}

func lookupFirstEnv(keys ...string) (string, bool) {
	for _, key := range keys {
		if value, ok := os.LookupEnv(key); ok {
			return value, true
		}
	}
	return "", false
}

func firstNonEmptyEnv(keys ...string) string {
	for _, key := range keys {
		if value := strings.TrimSpace(os.Getenv(key)); value != "" {
			return value
		}
	}
	return ""
}

func writeFileAtomic(path string, data []byte, mode os.FileMode) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".model.json-*")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)
	if err := tmp.Chmod(mode); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpPath, path)
}
