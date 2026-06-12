package control

import "github.com/iamlovingit/clawmanager-agent/internal/gateway"

type Config = gateway.Config
type CreateGatewayRequest = gateway.CreateGatewayRequest
type CreateGatewayResponse = gateway.CreateGatewayResponse
type GatewayStartSpec = gateway.GatewayStartSpec
type HeartbeatPayload = gateway.HeartbeatPayload
type ManagedProcess = gateway.ManagedProcess
type PortRange = gateway.PortRange

var NewGatewayManager = gateway.NewGatewayManager
var NewPortAllocator = gateway.NewPortAllocator
var ErrGatewayStartFailed = gateway.ErrGatewayStartFailed
