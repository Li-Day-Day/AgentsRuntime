package openclaw

import "testing"

func assertPlatformDefaults(t *testing.T, config map[string]any) {
	t.Helper()
	gateway := objectAt(t, config, "gateway")
	if gateway["port"] != float64(20003) {
		t.Fatalf("gateway.port = %#v, want 20003", gateway["port"])
	}
	cron := objectAt(t, config, "cron")
	if cron["enabled"] != true || cron["maxConcurrentRuns"] != float64(2) || cron["sessionRetention"] != "24h" {
		t.Fatalf("cron = %#v, want managed defaults", cron)
	}
	runLog := objectAt(t, cron, "runLog")
	if runLog["keepLines"] != float64(2000) || runLog["maxBytes"] != "2mb" {
		t.Fatalf("cron.runLog = %#v, want managed defaults", runLog)
	}
	update := objectAt(t, config, "update")
	if update["checkOnStart"] != false || objectAt(t, update, "auto")["enabled"] != false {
		t.Fatalf("update = %#v, want managed defaults", update)
	}
	internal := objectAt(t, objectAt(t, config, "hooks"), "internal")
	if internal["enabled"] != true {
		t.Fatalf("hooks.internal.enabled = %#v, want true", internal["enabled"])
	}
	sessionMemory := objectAt(t, objectAt(t, internal, "entries"), "session-memory")
	if sessionMemory["enabled"] != true || sessionMemory["messages"] != float64(50) {
		t.Fatalf("hooks.internal.entries.session-memory = %#v, want managed defaults", sessionMemory)
	}
	session := objectAt(t, config, "session")
	reset := objectAt(t, session, "reset")
	maintenance := objectAt(t, session, "maintenance")
	if reset["idleMinutes"] != float64(10080) || reset["mode"] != "idle" || maintenance["maxEntries"] != float64(2000) || maintenance["pruneAfter"] != "180d" {
		t.Fatalf("session = %#v, want managed defaults", session)
	}
}
