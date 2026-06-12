package openclaw

import (
	"encoding/json"
	"fmt"
	"strings"
)

func parseLLMModelIDs(raw string) ([]string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	if strings.HasPrefix(raw, "[") {
		var parsed []any
		if err := json.Unmarshal([]byte(raw), &parsed); err != nil {
			modelIDs := parseDelimitedLLMModelIDs(strings.TrimSuffix(strings.TrimPrefix(raw, "["), "]"))
			if len(modelIDs) == 0 {
				return nil, fmt.Errorf("parse CLAWMANAGER_LLM_MODEL array: %w", err)
			}
			return modelIDs, nil
		}
		modelIDs := uniqueLLMModelIDs(parsed)
		if len(modelIDs) == 0 {
			return nil, fmt.Errorf("parse CLAWMANAGER_LLM_MODEL array: no model ids found")
		}
		return modelIDs, nil
	}
	return []string{raw}, nil
}

func parseDelimitedLLMModelIDs(raw string) []string {
	parts := strings.Split(raw, ",")
	values := make([]any, 0, len(parts))
	for _, part := range parts {
		id := strings.Trim(strings.TrimSpace(part), `"'`)
		if id != "" {
			values = append(values, id)
		}
	}
	return uniqueLLMModelIDs(values)
}

func uniqueLLMModelIDs(values []any) []string {
	seen := make(map[string]struct{}, len(values))
	modelIDs := make([]string, 0, len(values))
	for _, value := range values {
		id := strings.TrimSpace(fmt.Sprint(value))
		if id == "" {
			continue
		}
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}
		modelIDs = append(modelIDs, id)
	}
	return modelIDs
}
