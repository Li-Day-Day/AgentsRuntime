package gateway

import (
	"encoding/json"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
)

const (
	teamConfigJSONEnv = "CLAWMANAGER_TEAM_CONFIG_JSON"
	teamConfigPathEnv = "CLAWMANAGER_TEAM_CONFIG_PATH"
	teamSharedDirEnv  = "CLAWMANAGER_TEAM_SHARED_DIR"
)

func ApplyRequestEnvironment(env []string, req CreateGatewayRequest) []string {
	env = applyEnvironmentMap(env, req.Env)
	env = applyEnvironmentMap(env, req.Environment)
	return env
}

func ApplyLiteTeamConfigEnvironment(env []string, req CreateGatewayRequest, workspacePath string) []string {
	configPath, sharedDir, ok := LiteTeamEnvironment(req, workspacePath)
	if !ok {
		return env
	}
	if configPath != "" {
		env = setEnv(env, teamConfigPathEnv, configPath)
	}
	if sharedDir != "" {
		env = setEnv(env, teamSharedDirEnv, sharedDir)
	}
	return env
}

func WriteLiteTeamConfigJSON(req CreateGatewayRequest, workspacePath string) error {
	raw, ok := requestEnvValue(req, teamConfigJSONEnv)
	if !ok || strings.TrimSpace(raw) == "" {
		return nil
	}
	configPath, _, ok := LiteTeamEnvironment(req, workspacePath)
	if !ok {
		return nil
	}
	if configPath == "" {
		return nil
	}
	var payload any
	if err := json.Unmarshal([]byte(raw), &payload); err != nil {
		return fmt.Errorf("parse %s: %w", teamConfigJSONEnv, err)
	}
	data, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal lite team config: %w", err)
	}
	data = append(data, '\n')

	filePath := filepath.FromSlash(configPath)
	if err := os.MkdirAll(filepath.Dir(filePath), 0o750); err != nil {
		return fmt.Errorf("create lite team config dir: %w", err)
	}
	if err := os.WriteFile(filePath, data, 0o600); err != nil {
		return fmt.Errorf("write lite team config: %w", err)
	}
	if err := os.Chmod(filePath, 0o600); err != nil {
		return fmt.Errorf("chmod lite team config: %w", err)
	}
	if err := ChownWorkspace(filepath.Dir(filePath), req.UID, req.GID); err != nil {
		return fmt.Errorf("chown lite team config dir: %w", err)
	}
	if err := ChownWorkspace(filePath, req.UID, req.GID); err != nil {
		return fmt.Errorf("chown lite team config: %w", err)
	}
	return nil
}

func LiteTeamEnvironment(req CreateGatewayRequest, workspacePath string) (string, string, bool) {
	configJSON, hasConfigJSON := requestEnvValue(req, teamConfigJSONEnv)
	teamEnabled, hasTeamEnabled := requestEnvValue(req, "CLAWMANAGER_TEAM_ENABLED")
	if (!hasConfigJSON || strings.TrimSpace(configJSON) == "") && (!hasTeamEnabled || !truthyTeamEnv(teamEnabled)) {
		return "", "", false
	}
	configPath, _ := requestEnvValue(req, teamConfigPathEnv)
	configPath = strings.TrimSpace(configPath)
	sharedDir, _ := requestEnvValue(req, teamSharedDirEnv)
	sharedDir = strings.TrimSpace(sharedDir)
	if sharedDir == "" || isDefaultTeamSharedDir(sharedDir) {
		sharedDir = envPathJoin(workspacePath, "team")
	}

	switch {
	case configPath != "":
		if sharedDir == "" || isDefaultTeamSharedDir(sharedDir) {
			sharedDir = envPathDir(configPath)
		}
	case sharedDir != "":
		if hasConfigJSON && strings.TrimSpace(configJSON) != "" {
			configPath = envPathJoin(sharedDir, "team.json")
		}
	default:
		sharedDir = envPathJoin(workspacePath, "team")
		if hasConfigJSON && strings.TrimSpace(configJSON) != "" {
			configPath = envPathJoin(sharedDir, "team.json")
		}
	}
	return configPath, sharedDir, true
}

func applyEnvironmentMap(env []string, values map[string]string) []string {
	if len(values) == 0 {
		return env
	}
	keys := make([]string, 0, len(values))
	for key := range values {
		if key != "" && !strings.Contains(key, "=") {
			keys = append(keys, key)
		}
	}
	sort.Strings(keys)
	for _, key := range keys {
		env = setEnv(env, key, values[key])
	}
	return env
}

func requestEnvValue(req CreateGatewayRequest, keys ...string) (string, bool) {
	for _, key := range keys {
		if req.Environment != nil {
			if value, ok := req.Environment[key]; ok {
				return value, true
			}
		}
		if req.Env != nil {
			if value, ok := req.Env[key]; ok {
				return value, true
			}
		}
	}
	return "", false
}

func envPathJoin(base string, elems ...string) string {
	if usesWindowsSeparators(base) {
		parts := append([]string{base}, elems...)
		return filepath.Join(parts...)
	}
	parts := append([]string{base}, elems...)
	return path.Join(parts...)
}

func envPathDir(value string) string {
	if usesWindowsSeparators(value) {
		return filepath.Dir(value)
	}
	return path.Dir(value)
}

func usesWindowsSeparators(value string) bool {
	return strings.Contains(value, `\`) || filepath.VolumeName(value) != ""
}

func isDefaultTeamSharedDir(value string) bool {
	return filepath.ToSlash(filepath.Clean(strings.TrimSpace(value))) == "/team"
}

func truthyTeamEnv(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "1", "true", "yes", "y", "on", "enabled":
		return true
	default:
		return false
	}
}
