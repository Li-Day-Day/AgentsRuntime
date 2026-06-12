package main

import "testing"

func TestSelectModePrefersRuntimePodAgent(t *testing.T) {
	t.Setenv("RUNTIME_AGENT_CONTROL_TOKEN", "control")
	t.Setenv("CLAWMANAGER_AGENT_ENABLED", "true")
	t.Setenv("CLAWMANAGER_RUNTIME_TYPE", "hermes")

	if got := selectMode(); got != modeRuntimePod {
		t.Fatalf("selectMode() = %s, want %s", got, modeRuntimePod)
	}
}

func TestSelectModeUsesInstanceAgentWhenEnabled(t *testing.T) {
	t.Setenv("CLAWMANAGER_AGENT_ENABLED", "true")
	t.Setenv("CLAWMANAGER_RUNTIME_TYPE", "hermes")

	if got := selectMode(); got != modeInstance {
		t.Fatalf("selectMode() = %s, want %s", got, modeInstance)
	}
}

func TestSelectModeDisabledWithoutRuntimeTokens(t *testing.T) {
	if got := selectMode(); got != modeDisabled {
		t.Fatalf("selectMode() = %s, want %s", got, modeDisabled)
	}
}

func TestSelectModeDoesNotRunInstanceAgentForOtherRuntime(t *testing.T) {
	t.Setenv("CLAWMANAGER_AGENT_ENABLED", "true")
	t.Setenv("CLAWMANAGER_RUNTIME_TYPE", "openclaw")

	if got := selectMode(); got != modeDisabled {
		t.Fatalf("selectMode() = %s, want %s", got, modeDisabled)
	}
}
