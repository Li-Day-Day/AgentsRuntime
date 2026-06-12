package instanceagent

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

type Session struct {
	AgentID                    string    `json:"agent_id"`
	SessionToken               string    `json:"session_token"`
	RegisteredAt               time.Time `json:"registered_at"`
	HeartbeatIntervalSeconds   int       `json:"heartbeat_interval_seconds,omitempty"`
	CommandPollIntervalSeconds int       `json:"command_poll_interval_seconds,omitempty"`
}

type registerResponse struct {
	SessionToken               string `json:"session_token"`
	HeartbeatIntervalSeconds   int    `json:"heartbeat_interval_seconds"`
	CommandPollIntervalSeconds int    `json:"command_poll_interval_seconds"`
	StateReportIntervalSeconds int    `json:"state_report_interval_seconds"`
	SkillScanIntervalSeconds   int    `json:"skill_scan_interval_seconds"`
}

type heartbeatResponse struct {
	HasPendingCommand          bool `json:"has_pending_command"`
	HeartbeatIntervalSeconds   int  `json:"heartbeat_interval_seconds"`
	CommandPollIntervalSeconds int  `json:"command_poll_interval_seconds"`
}

type RuntimeInfo struct {
	OpenClawStatus  string `json:"openclaw_status"`
	OpenClawPID     int    `json:"openclaw_pid"`
	OpenClawVersion string `json:"openclaw_version"`
}

type SystemInfo struct {
	Runtime            string             `json:"runtime"`
	OS                 string             `json:"os"`
	OSName             string             `json:"os_name,omitempty"`
	OSVersion          string             `json:"os_version,omitempty"`
	Kernel             string             `json:"kernel,omitempty"`
	Arch               string             `json:"arch,omitempty"`
	Hostname           string             `json:"hostname,omitempty"`
	DesktopBase        string             `json:"desktop_base"`
	SampledAt          time.Time          `json:"sampled_at"`
	CPUCores           float64            `json:"cpu_cores"`
	CPUUsagePercent    float64            `json:"cpu_usage_percent"`
	LoadAverage1       float64            `json:"load_average_1"`
	LoadAverage5       float64            `json:"load_average_5"`
	LoadAverage15      float64            `json:"load_average_15"`
	MemoryTotal        int64              `json:"memory_total_bytes"`
	MemoryFree         int64              `json:"memory_free_bytes"`
	MemoryAvailable    int64              `json:"memory_available_bytes"`
	MemoryUsed         int64              `json:"memory_used_bytes"`
	MemoryUsagePercent float64            `json:"memory_usage_percent"`
	DiskTotalBytes     int64              `json:"disk_total_bytes"`
	DiskFreeBytes      int64              `json:"disk_free_bytes"`
	DiskUsedBytes      int64              `json:"disk_used_bytes"`
	DiskUsagePercent   float64            `json:"disk_usage_percent"`
	DiskLimitBytes     int64              `json:"disk_limit_bytes,omitempty"`
	NetworkRxBytes     int64              `json:"network_rx_bytes"`
	NetworkTxBytes     int64              `json:"network_tx_bytes"`
	NetworkInterfaces  []NetworkInterface `json:"network_interfaces,omitempty"`
	CPU                map[string]any     `json:"cpu,omitempty"`
	Memory             map[string]any     `json:"memory,omitempty"`
	Disk               map[string]any     `json:"disk,omitempty"`
	Network            map[string]any     `json:"network,omitempty"`
}

type NetworkInterface struct {
	Name      string   `json:"name"`
	Status    string   `json:"status"`
	Addresses []string `json:"addresses,omitempty"`
	RXBytes   int64    `json:"rx_bytes"`
	TXBytes   int64    `json:"tx_bytes"`
}

type HealthInfo map[string]any

type StateReport struct {
	AgentID    string      `json:"agent_id"`
	ReportedAt time.Time   `json:"reported_at"`
	Runtime    RuntimeInfo `json:"runtime"`
	SystemInfo SystemInfo  `json:"system_info"`
	Health     HealthInfo  `json:"health"`
}

type HeartbeatBody struct {
	AgentID        string         `json:"agent_id"`
	Timestamp      time.Time      `json:"timestamp"`
	OpenClawStatus string         `json:"openclaw_status"`
	Summary        map[string]any `json:"summary"`
}

type SkillInfo struct {
	SkillID      string         `json:"skill_id"`
	SkillVersion string         `json:"skill_version"`
	Identifier   string         `json:"identifier"`
	InstallPath  string         `json:"install_path"`
	ContentMD5   string         `json:"content_md5"`
	Source       string         `json:"source"`
	Type         string         `json:"type"`
	SizeBytes    int64          `json:"size_bytes"`
	FileCount    int            `json:"file_count"`
	Metadata     map[string]any `json:"metadata"`
}

type SkillInventoryBody struct {
	AgentID    string      `json:"agent_id"`
	ReportedAt time.Time   `json:"reported_at"`
	Mode       string      `json:"mode"`
	Trigger    string      `json:"trigger"`
	Skills     []SkillInfo `json:"skills"`
}

type Command struct {
	ID      string         `json:"id"`
	Type    string         `json:"type"`
	Payload map[string]any `json:"payload"`
	Raw     map[string]any `json:"-"`
}

func (c *Command) UnmarshalJSON(data []byte) error {
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}

	c.Raw = raw
	c.ID = firstString(raw, "id", "command_id")
	c.Type = firstString(raw, "type", "command_type", "name")
	c.Payload = map[string]any{}

	if payload, ok := raw["payload"].(map[string]any); ok {
		c.Payload = payload
	} else if payload, ok := raw["params"].(map[string]any); ok {
		c.Payload = payload
	}

	return nil
}

type nextCommandResponse struct {
	Command *Command `json:"command"`
}

type commandFinishBody struct {
	AgentID      string         `json:"agent_id"`
	Status       string         `json:"status"`
	FinishedAt   time.Time      `json:"finished_at"`
	Result       map[string]any `json:"result"`
	ErrorMessage string         `json:"error_message"`
}

func firstString(raw map[string]any, keys ...string) string {
	for _, key := range keys {
		value, ok := raw[key]
		if !ok || value == nil {
			continue
		}
		switch typed := value.(type) {
		case string:
			if typed != "" {
				return typed
			}
		case float64:
			return strings.TrimSuffix(strings.TrimSuffix(fmt.Sprintf("%.0f", typed), ".0"), ".")
		default:
			return fmt.Sprint(typed)
		}
	}
	return ""
}
