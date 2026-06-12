//go:build windows

package gateway

func ChownWorkspace(string, int, int) error {
	return nil
}
