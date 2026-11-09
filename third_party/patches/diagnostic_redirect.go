// This project-local patch only guards its reserved diagnostic credentials.
// The upstream executor, selected transports, TLS profiles and business routes
// remain unchanged. See the adjacent manifest for the exact upstream revision.
package helps

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"

	"github.com/gin-gonic/gin"

	cliproxyauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
)

const CPA2Sub2APIDiagnosticGuardFeatureID = "cpa2sub2api-diagnostic-no-redirect-v1"
const cpa2sub2apiDiagnosticPrefixesEnv = "CPA2SUB2API_DIAGNOSTIC_PREFIXES"
const cpa2sub2apiAutomaticOutputHeader = "X-CPA2SUB2API-Auto-Output-Cap"
const cpa2sub2apiAutomaticOutputCap = 16

func init() {
	// A feature query finishes before main starts any config, auth or network
	// work. Older upstream binaries do not implement this opt-in query.
	if os.Getenv("CPA2SUB2API_GUARD_FEATURE_QUERY") == "1" {
		_ = json.NewEncoder(os.Stdout).Encode(map[string]interface{}{
			"feature_id":    CPA2Sub2APIDiagnosticGuardFeatureID,
			"source_commit": "91e0dee827abf476f9f86246f68c500a112f7155",
			"prefix_env":    cpa2sub2apiDiagnosticPrefixesEnv,
			"automatic_output_cap": cpa2sub2apiAutomaticOutputCap,
			"automatic_output_header": cpa2sub2apiAutomaticOutputHeader,
		})
		os.Exit(0)
	}
}

func isCPA2Sub2APIDiagnosticPrefix(prefix string) bool {
	if len(prefix) != 41 || prefix[0] != 'd' {
		return false
	}
	for _, b := range []byte(prefix[1:]) {
		if !(b >= '0' && b <= '9') && !(b >= 'a' && b <= 'f') {
			return false
		}
	}
	return true
}

func cpa2sub2apiDiagnosticPrefixes() (map[string]struct{}, bool) {
	raw, present := os.LookupEnv(cpa2sub2apiDiagnosticPrefixesEnv)
	if !present {
		return nil, false
	}
	var prefixes []string
	if err := json.Unmarshal([]byte(raw), &prefixes); err != nil || prefixes == nil {
		return nil, false
	}
	allowed := make(map[string]struct{}, len(prefixes))
	for _, prefix := range prefixes {
		if !isCPA2Sub2APIDiagnosticPrefix(prefix) {
			return nil, false
		}
		if _, duplicate := allowed[prefix]; duplicate {
			return nil, false
		}
		allowed[prefix] = struct{}{}
	}
	return allowed, true
}

type cpa2sub2apiBlockedDiagnosticTransport struct{}

func (cpa2sub2apiBlockedDiagnosticTransport) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, errors.New("diagnostic_redirect_policy_unavailable")
}

func applyCPA2Sub2APIDiagnosticRedirectPolicy(client *http.Client, auth *cliproxyauth.Auth) *http.Client {
	if client == nil || auth == nil || !isCPA2Sub2APIDiagnosticPrefix(auth.Prefix) {
		return client
	}
	clone := *client
	allowed, valid := cpa2sub2apiDiagnosticPrefixes()
	_, selected := allowed[auth.Prefix]
	if !valid || !selected {
		// Never turn a malformed/missing allowlist into an ordinary request.
		// Reserved d<route-id> credentials are exclusively diagnostic.
		clone.Transport = cpa2sub2apiBlockedDiagnosticTransport{}
	}
	clone.CheckRedirect = func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}
	return &clone
}

// Keep the attempt state on this execution's Gin context, not on an HTTP
// client: native retry paths construct new clients for the same execution.
type cpa2sub2apiWireBudget struct { used atomic.Bool }
var cpa2sub2apiWireBudgetMutex sync.Mutex
const cpa2sub2apiWireBudgetKey = "cpa2sub2api.native-wire-budget.v1"

type cpa2sub2apiAutomaticTransport struct {
	next http.RoundTripper
	budget *cpa2sub2apiWireBudget
	hint string
}

func applyCPA2Sub2APIDiagnosticWirePolicy(ctx context.Context, client *http.Client, auth *cliproxyauth.Auth) *http.Client {
	if ctx == nil || client == nil || auth == nil || !isCPA2Sub2APIDiagnosticPrefix(auth.Prefix) { return client }
	c, ok := ctx.Value("gin").(*gin.Context)
	if !ok || c == nil || c.Request == nil { return client }
	hints := c.Request.Header.Values(cpa2sub2apiAutomaticOutputHeader)
	if len(hints) == 0 { return client } // An explicit manual diagnostic.
	hint := "invalid"
	if len(hints) == 1 && hints[0] == "16" { hint = "16" }
	cpa2sub2apiWireBudgetMutex.Lock()
	value, exists := c.Get(cpa2sub2apiWireBudgetKey)
	budget, valid := value.(*cpa2sub2apiWireBudget)
	if !exists { budget = &cpa2sub2apiWireBudget{}; c.Set(cpa2sub2apiWireBudgetKey, budget) } else if !valid || budget == nil { hint = "invalid" }
	cpa2sub2apiWireBudgetMutex.Unlock()
	clone := *client
	next := clone.Transport
	if next == nil { next = http.DefaultTransport }
	clone.Transport = &cpa2sub2apiAutomaticTransport{next: next, budget: budget, hint: hint}
	return &clone
}

func cpa2sub2apiAutomaticRejected(request *http.Request, code string) (*http.Response, error) {
	if request.Body != nil { _ = request.Body.Close() }
	data, _ := json.Marshal(map[string]interface{}{"error": map[string]string{"code": code, "message": code}})
	return &http.Response{StatusCode: http.StatusBadRequest, Status: "400 Bad Request", Request: request,
		Header: http.Header{"Content-Type": []string{"application/json"}}, Body: io.NopCloser(bytes.NewReader(data)), ContentLength: int64(len(data))}, nil
}

// Token decoding rejects duplicate keys at every depth, rather than letting
// the last JSON occurrence override the field that was inspected.
func cpa2sub2apiUniqueJSON(decoder *json.Decoder, depth int) (interface{}, error) {
	if depth > 64 { return nil, errors.New("depth") }
	token, err := decoder.Token()
	if err != nil { return nil, err }
	delim, compound := token.(json.Delim)
	if !compound { return token, nil }
	if delim == '{' {
		result := map[string]interface{}{}
		for decoder.More() {
			keyToken, err := decoder.Token(); if err != nil { return nil, err }
			key, ok := keyToken.(string); if !ok { return nil, errors.New("key") }
			if _, duplicate := result[key]; duplicate { return nil, errors.New("duplicate") }
			value, err := cpa2sub2apiUniqueJSON(decoder, depth+1); if err != nil { return nil, err }; result[key] = value
		}
		closing, err := decoder.Token(); if err != nil || closing != json.Delim('}') { return nil, errors.New("object") }
		return result, nil
	}
	if delim == '[' {
		result := []interface{}{}
		for decoder.More() { value, err := cpa2sub2apiUniqueJSON(decoder, depth+1); if err != nil { return nil, err }; result = append(result, value) }
		closing, err := decoder.Token(); if err != nil || closing != json.Delim(']') { return nil, errors.New("array") }
		return result, nil
	}
	return nil, errors.New("delimiter")
}

func cpa2sub2apiUnsafeProfile(value interface{}) bool {
	switch item := value.(type) {
	case map[string]interface{}:
		for key, child := range item {
			switch strings.ToLower(key) {
			case "thinking", "thinkingconfig", "reasoning", "reasoning_effort", "thought_signature", "thoughtsignature", "signature", "tools", "functions", "tool_choice", "function_call", "modalities", "responsemodalities", "response_modalities", "audio", "speechconfig", "speech_config", "image_config", "imageconfig":
				if child != nil { return true }
			case "type":
				if kind, ok := child.(string); ok && (kind == "thinking" || kind == "redacted_thinking") { return true }
			case "n", "candidatecount", "candidate_count":
				count, ok := child.(json.Number); if !ok || count.String() != "1" { return true }
			}
			if cpa2sub2apiUnsafeProfile(child) { return true }
		}
	case []interface{}:
		for _, child := range item { if cpa2sub2apiUnsafeProfile(child) { return true } }
	}
	return false
}

func cpa2sub2apiBoundField(object map[string]interface{}, field string) bool {
	if prior, exists := object[field]; exists {
		number, ok := prior.(json.Number); if !ok { return false }
		count, err := number.Int64(); if err != nil || count <= 0 { return false }
	}
	object[field] = cpa2sub2apiAutomaticOutputCap
	return true
}

var cpa2sub2apiGeminiInference = regexp.MustCompile(`/(?:v1beta|v1)/models/[^:]+:(?:generateContent|streamGenerateContent)$`)

func (transport *cpa2sub2apiAutomaticTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	if transport.hint != "16" || transport.budget == nil { return cpa2sub2apiAutomaticRejected(request, "automatic_probe_output_hint_invalid") }
	if request.Method != http.MethodPost || request.URL == nil { return cpa2sub2apiAutomaticRejected(request, "automatic_probe_unsupported_protocol") }
	path := request.URL.Path
	field := ""
	gemini := cpa2sub2apiGeminiInference.MatchString(path)
	switch {
	case gemini: field = "maxOutputTokens"
	case strings.HasSuffix(path, "/chat/completions"), strings.HasSuffix(path, "/messages"): field = "max_tokens"
	case strings.HasSuffix(path, "/responses"): field = "max_output_tokens"
	default: return cpa2sub2apiAutomaticRejected(request, "automatic_probe_unsupported_protocol")
	}
	if encoding := request.Header.Get("Content-Encoding"); encoding != "" && encoding != "identity" { return cpa2sub2apiAutomaticRejected(request, "automatic_probe_unsafe_profile") }
	if request.Body == nil { return cpa2sub2apiAutomaticRejected(request, "automatic_probe_unsafe_payload") }
	data, err := io.ReadAll(io.LimitReader(request.Body, 64*1024+1))
	_ = request.Body.Close()
	if err != nil || len(data) > 64*1024 { return cpa2sub2apiAutomaticRejected(request, "automatic_probe_unsafe_payload") }
	decoder := json.NewDecoder(bytes.NewReader(data)); decoder.UseNumber()
	value, err := cpa2sub2apiUniqueJSON(decoder, 0)
	object, objectOK := value.(map[string]interface{})
	if err != nil || !objectOK { return cpa2sub2apiAutomaticRejected(request, "automatic_probe_unsafe_payload") }
	if _, err := decoder.Token(); err != io.EOF { return cpa2sub2apiAutomaticRejected(request, "automatic_probe_unsafe_payload") }
	if cpa2sub2apiUnsafeProfile(object) { return cpa2sub2apiAutomaticRejected(request, "automatic_probe_unsafe_profile") }
	target := object
	if gemini {
		if configuration, exists := object["generationConfig"]; exists {
			var ok bool; target, ok = configuration.(map[string]interface{}); if !ok { return cpa2sub2apiAutomaticRejected(request, "automatic_probe_unsafe_payload") }
		} else { target = map[string]interface{}{}; object["generationConfig"] = target }
	}
	for _, alternate := range []string{"max_tokens", "max_completion_tokens", "max_output_tokens"} {
		if alternate == field { continue }
		if _, exists := object[alternate]; exists && !cpa2sub2apiBoundField(object, alternate) { return cpa2sub2apiAutomaticRejected(request, "automatic_probe_unsafe_payload") }
	}
	if !cpa2sub2apiBoundField(target, field) { return cpa2sub2apiAutomaticRejected(request, "automatic_probe_unsafe_payload") }
	data, err = json.Marshal(object); if err != nil { return cpa2sub2apiAutomaticRejected(request, "automatic_probe_unsafe_payload") }
	// Claim immediately before any source I/O. Source failure and cancellation
	// do not permit another native executor/client attempt for this execution.
	if !transport.budget.used.CompareAndSwap(false, true) { return cpa2sub2apiAutomaticRejected(request, "automatic_probe_wire_budget_exhausted") }
	clone := request.Clone(request.Context())
	clone.Header = request.Header.Clone()
	for key := range clone.Header {
		for _, private := range []string{cpa2sub2apiAutomaticOutputHeader, "Idempotency-Key", "X-Idempotency-Key", "X-Relay-Automatic-Probe", "X-Relay-Exact-Key", "X-Relay-Generation"} {
			if strings.EqualFold(key, private) { delete(clone.Header, key) }
		}
	}
	clone.Header.Set("Content-Length", strconv.Itoa(len(data)))
	clone.Body = io.NopCloser(bytes.NewReader(data)); clone.ContentLength = int64(len(data)); clone.GetBody = nil
	return transport.next.RoundTrip(clone)
}

func (transport *cpa2sub2apiAutomaticTransport) CloseIdleConnections() {
	if closer, ok := transport.next.(interface{ CloseIdleConnections() }); ok { closer.CloseIdleConnections() }
}
