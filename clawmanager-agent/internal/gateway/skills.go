package gateway

import (
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

func BuildSkillReportPayload(cfg Config, manager *GatewayManager, podID int, mode string) SkillReportPayload {
	states := manager.GatewayStates()
	instances := make([]SkillInstanceReport, 0, len(states))
	for _, state := range states {
		instances = append(instances, SkillInstanceReport{
			InstanceID:    state.InstanceID,
			WorkspacePath: state.WorkspacePath,
			Skills:        scanWorkspaceSkills(state.WorkspacePath),
		})
	}
	return SkillReportPayload{
		PodID:       podID,
		RuntimeType: cfg.RuntimeType,
		Namespace:   cfg.Namespace,
		PodName:     cfg.PodName,
		ReportedAt:  time.Now().UTC(),
		Mode:        mode,
		Instances:   instances,
	}
}

func scanWorkspaceSkills(workspacePath string) []SkillRecord {
	roots := []string{
		filepath.Join(workspacePath, "skills"),
		filepath.Join(workspacePath, "home", ".hermes", "skills"),
		filepath.Join(workspacePath, "home", ".openclaw", "workspace", "skills"),
		filepath.Join(workspacePath, ".openclaw", "workspace", "skills"),
	}
	seen := map[string]bool{}
	var skills []SkillRecord
	for _, root := range roots {
		entries, err := os.ReadDir(root)
		if err != nil {
			continue
		}
		for _, entry := range entries {
			if !entry.IsDir() {
				continue
			}
			installPath := filepath.Join(root, entry.Name())
			if seen[installPath] {
				continue
			}
			seen[installPath] = true
			skills = append(skills, readSkillRecord(installPath, entry.Name()))
		}
	}
	sort.Slice(skills, func(i, j int) bool {
		return skills[i].InstallPath < skills[j].InstallPath
	})
	return skills
}

func readSkillRecord(installPath, fallbackID string) SkillRecord {
	record := SkillRecord{
		SkillID:     fallbackID,
		Identifier:  fallbackID,
		InstallPath: installPath,
		Source:      "runtime",
		Type:        "agent-skill",
	}
	for _, metaName := range []string{"skill.json", "openclaw.skill.json"} {
		data, err := os.ReadFile(filepath.Join(installPath, metaName))
		if err != nil {
			continue
		}
		var meta map[string]any
		if json.Unmarshal(data, &meta) != nil {
			continue
		}
		if value := stringField(meta, "skill_id", "id", "name"); value != "" {
			record.SkillID = value
			record.Identifier = value
		}
		if value := stringField(meta, "identifier"); value != "" {
			record.Identifier = value
		}
		if value := stringField(meta, "skill_version", "version"); value != "" {
			record.SkillVersion = value
		}
		break
	}
	record.ContentMD5 = contentMD5(filepath.Join(installPath, "SKILL.md"))
	return record
}

func stringField(values map[string]any, keys ...string) string {
	for _, key := range keys {
		raw, ok := values[key]
		if !ok {
			continue
		}
		value, ok := raw.(string)
		if ok && strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func contentMD5(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	sum := md5.Sum(data)
	return hex.EncodeToString(sum[:])
}
