package main

import (
	"context"
	"errors"
	"log"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"

	runtimeagent "github.com/iamlovingit/clawmanager-agent/internal/agent"
	"github.com/iamlovingit/clawmanager-agent/internal/instanceagent"
)

var version = "dev"

type agentMode string

const (
	modeRuntimePod agentMode = "runtime-pod"
	modeInstance   agentMode = "instance"
	modeDisabled   agentMode = "disabled"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	switch selectMode() {
	case modeRuntimePod:
		cfg, err := runtimeagent.LoadConfigFromEnv()
		if err != nil {
			log.Fatalf("load runtime agent config: %v", err)
		}
		if err := runtimeagent.NewAgent(cfg).Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
			log.Fatalf("run runtime agent: %v", err)
		}
	case modeInstance:
		logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
		cfg, err := instanceagent.LoadConfig(version)
		if err != nil {
			logger.Error("load instance agent config", "error", err)
			os.Exit(2)
		}
		if !cfg.Enabled {
			logger.Info("ClawManager instance agent disabled")
			return
		}
		if err := instanceagent.New(cfg, logger).Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
			logger.Error("run instance agent", "error", err)
			os.Exit(1)
		}
	case modeDisabled:
		log.Print("ClawManager shared agent disabled")
	}
}

func selectMode() agentMode {
	if runtimeagent.RuntimeAgentModeEnabled() {
		return modeRuntimePod
	}
	if strings.EqualFold(os.Getenv("CLAWMANAGER_AGENT_ENABLED"), "true") && strings.EqualFold(instanceRuntimeType(), "hermes") {
		return modeInstance
	}
	return modeDisabled
}

func instanceRuntimeType() string {
	for _, key := range []string{"CLAWMANAGER_RUNTIME_TYPE", "CLAWMANAGER_AGENT_RUNTIME_TYPE"} {
		if value := strings.TrimSpace(os.Getenv(key)); value != "" {
			return value
		}
	}
	return ""
}
