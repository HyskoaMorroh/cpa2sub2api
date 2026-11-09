package helps

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	cliproxyauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/httptransport"
)

var cpa2sub2apiTestPrefix = "d" + strings.Repeat("a", 40)

func cpa2sub2apiSetAllowed(t *testing.T, prefixes ...string) {
	t.Helper()
	raw, err := json.Marshal(prefixes)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv(cpa2sub2apiDiagnosticPrefixesEnv, string(raw))
}

func TestCPA2Sub2APIDiagnosticRedirectsNeverSendSecondRequest(t *testing.T) {
	cpa2sub2apiSetAllowed(t, cpa2sub2apiTestPrefix)
	constructors := map[string]func(context.Context, *config.Config, *cliproxyauth.Auth, time.Duration) *http.Client{
		"standard": NewProxyAwareHTTPClient,
		"utls":     NewUtlsHTTPClient,
	}
	for name, constructor := range constructors {
		for _, status := range []int{301, 302, 303, 307, 308} {
			t.Run(name+"/"+http.StatusText(status), func(t *testing.T) {
				var first, target atomic.Int64
				destination := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					target.Add(1)
					_, _ = w.Write([]byte("unexpected"))
				}))
				defer destination.Close()
				origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					first.Add(1)
					w.Header().Set("Location", destination.URL+"/second")
					w.WriteHeader(status)
					_, _ = w.Write([]byte("redirect refused"))
				}))
				defer origin.Close()
				client := constructor(context.Background(), &config.Config{},
					&cliproxyauth.Auth{Prefix: cpa2sub2apiTestPrefix}, 3*time.Second)
				defer client.CloseIdleConnections()
				request, _ := http.NewRequest("POST", origin.URL+"/first", strings.NewReader("{}"))
				response, err := client.Do(request)
				if err != nil {
					t.Fatal(err)
				}
				body, _ := io.ReadAll(response.Body)
				_ = response.Body.Close()
				if response.StatusCode != status || string(body) != "redirect refused" {
					t.Fatalf("last response was not retained: %d %q", response.StatusCode, body)
				}
				if first.Load() != 1 || target.Load() != 0 {
					t.Fatalf("unbounded wire requests: initial=%d redirected=%d", first.Load(), target.Load())
				}
			})
		}
	}
}

func TestCPA2Sub2APIDiagnosticMissingOrMalformedAllowlistIsZeroHTTP(t *testing.T) {
	for _, value := range []string{"missing", "", "null", "{}", "[false]", `["not-a-diagnostic-prefix"]`} {
		t.Run(value, func(t *testing.T) {
			if value == "missing" {
				old, found := os.LookupEnv(cpa2sub2apiDiagnosticPrefixesEnv)
				_ = os.Unsetenv(cpa2sub2apiDiagnosticPrefixesEnv)
				t.Cleanup(func() {
					if found {
						_ = os.Setenv(cpa2sub2apiDiagnosticPrefixesEnv, old)
					} else {
						_ = os.Unsetenv(cpa2sub2apiDiagnosticPrefixesEnv)
					}
				})
			} else {
				t.Setenv(cpa2sub2apiDiagnosticPrefixesEnv, value)
			}
			var requests atomic.Int64
			origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				_, _ = w.Write([]byte("must not be reached"))
			}))
			defer origin.Close()
			client := NewProxyAwareHTTPClient(context.Background(), &config.Config{},
				&cliproxyauth.Auth{Prefix: cpa2sub2apiTestPrefix}, time.Second)
			defer client.CloseIdleConnections()
			request, _ := http.NewRequest("POST", origin.URL, strings.NewReader("{}"))
			if response, err := client.Do(request); err == nil {
				_ = response.Body.Close()
				t.Fatal("invalid diagnostic policy was accepted")
			}
			if requests.Load() != 0 {
				t.Fatalf("invalid policy sent %d requests", requests.Load())
			}
		})
	}
}

func TestCPA2Sub2APIDiagnosticUnlistedAndDuplicatePrefixesAreZeroHTTP(t *testing.T) {
	for _, prefixes := range [][]string{{"d" + strings.Repeat("b", 40)}, {cpa2sub2apiTestPrefix, cpa2sub2apiTestPrefix}} {
		cpa2sub2apiSetAllowed(t, prefixes...)
		var requests atomic.Int64
		origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			requests.Add(1)
		}))
		client := NewUtlsHTTPClient(context.Background(), &config.Config{},
			&cliproxyauth.Auth{Prefix: cpa2sub2apiTestPrefix}, time.Second)
		request, _ := http.NewRequest("POST", origin.URL, strings.NewReader("{}"))
		if response, err := client.Do(request); err == nil {
			_ = response.Body.Close()
			t.Fatal("unlisted/duplicate policy was accepted")
		}
		client.CloseIdleConnections()
		origin.Close()
		if requests.Load() != 0 {
			t.Fatalf("invalid policy sent %d requests", requests.Load())
		}
	}
}

func TestCPA2Sub2APIDiagnosticPolicyPreservesBusinessAndDecoratorClientOptions(t *testing.T) {
	cpa2sub2apiSetAllowed(t, cpa2sub2apiTestPrefix)
	transport := http.DefaultTransport.(*http.Transport).Clone()
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 11 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return nil }}
	for _, prefix := range []string{"r" + strings.Repeat("a", 40), "p" + strings.Repeat("a", 40), ""} {
		if got := applyCPA2Sub2APIDiagnosticRedirectPolicy(client, &cliproxyauth.Auth{Prefix: prefix}); got != client {
			t.Fatal("business client identity or policy changed")
		}
	}
	guarded := applyCPA2Sub2APIDiagnosticRedirectPolicy(client, &cliproxyauth.Auth{Prefix: cpa2sub2apiTestPrefix})
	if guarded == client || guarded.Transport != client.Transport || guarded.Timeout != client.Timeout {
		t.Fatal("diagnostic policy replaced transport/pool/timeout")
	}
	contextWithDecorator := httptransport.WithDecorator(context.Background(),
		func(next http.RoundTripper) http.RoundTripper { return next })
	decorated := NewProxyAwareHTTPClient(contextWithDecorator, &config.Config{},
		&cliproxyauth.Auth{Prefix: cpa2sub2apiTestPrefix}, 7*time.Second)
	defer decorated.CloseIdleConnections()
	if decorated.CheckRedirect == nil || decorated.CheckRedirect(nil, nil) != http.ErrUseLastResponse ||
		decorated.Timeout != 7*time.Second {
		t.Fatal("decorator bypassed diagnostic policy")
	}
}

func TestCPA2Sub2APIDiagnosticFeatureQuery(t *testing.T) {
	command := exec.Command(os.Args[0], "-test.run=TestCPA2Sub2APIDiagnosticFeatureQuery")
	command.Env = append(os.Environ(), "CPA2SUB2API_GUARD_FEATURE_QUERY=1")
	output, err := command.Output()
	if err != nil {
		t.Fatal(err)
	}
	var feature map[string]interface{}
	if err := json.Unmarshal(output, &feature); err != nil {
		t.Fatal(err)
	}
	if feature["feature_id"] != CPA2Sub2APIDiagnosticGuardFeatureID ||
		feature["source_commit"] != "91e0dee827abf476f9f86246f68c500a112f7155" ||
		feature["automatic_output_cap"] != float64(16) ||
		feature["automatic_output_header"] != "X-CPA2SUB2API-Auto-Output-Cap" {
		t.Fatalf("unexpected feature reply %s", output)
	}
}

func cpa2sub2apiAutomaticContext(hint string) context.Context {
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	if hint != "absent" {
		c.Request.Header.Set("X-CPA2SUB2API-Auto-Output-Cap", hint)
	}
	return context.WithValue(context.Background(), "gin", c)
}

func TestCPA2Sub2APIDiagnosticAutomaticFinalPayloadCapAndOneAttempt(t *testing.T) {
	cpa2sub2apiSetAllowed(t, cpa2sub2apiTestPrefix)
	constructors := map[string]func(context.Context, *config.Config, *cliproxyauth.Auth, time.Duration) *http.Client{
		"standard": NewProxyAwareHTTPClient, "utls": NewUtlsHTTPClient,
	}
	for name, constructor := range constructors {
		for _, fixture := range []struct { path, body, field string }{
			{"/v1/chat/completions", `{"model":"gpt-highest","messages":[],"max_tokens":1024,"max_completion_tokens":512}`, "max_tokens"},
			{"/v1/messages", `{"model":"claude-highest","messages":[],"max_tokens":1024}`, "max_tokens"},
			{"/backend-api/codex/responses", `{"model":"codex-highest","input":[]}`, "max_output_tokens"},
			{"/v1beta/models/gemini-highest:streamGenerateContent?alt=sse", `{"contents":[],"generationConfig":{"maxOutputTokens":4096}}`, "generationConfig"},
		} {
			t.Run(name+fixture.path, func(t *testing.T) {
				var posts atomic.Int64
				origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					posts.Add(1)
					var body map[string]interface{}
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil { t.Error(err) }
					if fixture.field == "generationConfig" {
						if body[fixture.field].(map[string]interface{})["maxOutputTokens"] != float64(16) { t.Error("Gemini final output cap absent") }
						if !strings.Contains(r.URL.Path, "gemini-highest:") { t.Error("highest Gemini model changed") }
					} else if body[fixture.field] != float64(16) { t.Error("final output cap absent") }
					if fixture.field == "max_tokens" && body["max_completion_tokens"] != nil && body["max_completion_tokens"] != float64(16) { t.Error("alternate budget bypass") }
					var before map[string]interface{}
					_ = json.Unmarshal([]byte(fixture.body), &before)
					if before["model"] != body["model"] { t.Error("highest model changed") }
					if r.Header.Get("X-CPA2SUB2API-Auto-Output-Cap") != "" || r.Header.Get("Idempotency-Key") != "" { t.Error("private/replay header leaked") }
					w.WriteHeader(http.StatusServiceUnavailable)
				}))
				defer origin.Close()
				ctx := cpa2sub2apiAutomaticContext("16")
				for attempt := 0; attempt < 2; attempt++ {
					client := constructor(ctx, &config.Config{}, &cliproxyauth.Auth{Prefix: cpa2sub2apiTestPrefix}, time.Second)
					request, _ := http.NewRequest(http.MethodPost, origin.URL+fixture.path, strings.NewReader(fixture.body))
					request.Header.Set("Content-Type", "application/json")
					request.Header.Set("Idempotency-Key", "must-not-enable-replay")
					request.Header.Set("X-CPA2SUB2API-Auto-Output-Cap", "forged-outgoing-value")
					response, err := client.Do(request)
					if err != nil { t.Fatal(err) }
					data, _ := io.ReadAll(response.Body)
					_ = response.Body.Close()
					client.CloseIdleConnections()
					if attempt == 0 && response.StatusCode != 503 { t.Fatalf("source response changed: %d %s", response.StatusCode, data) }
					if attempt == 1 && (response.StatusCode != 400 || !strings.Contains(string(data), "automatic_probe_wire_budget_exhausted")) { t.Fatalf("second attempt accepted: %d %s", response.StatusCode, data) }
				}
				if posts.Load() != 1 { t.Fatalf("sent %d provider POSTs", posts.Load()) }
			})
		}
	}
}

func TestCPA2Sub2APIDiagnosticAutomaticUnsafeProfilesAreZeroHTTP(t *testing.T) {
	cpa2sub2apiSetAllowed(t, cpa2sub2apiTestPrefix)
	for _, fixture := range []struct { path, body, hint, encoding, code string }{
		{"/v1/messages", `{"model":"claude-highest","thinking":{"type":"enabled","budget_tokens":1024}}`, "16", "", "automatic_probe_unsafe_profile"},
		{"/v1beta/models/gemini-highest:generateContent", `{"contents":[],"generationConfig":{"responseModalities":["IMAGE"]}}`, "16", "", "automatic_probe_unsafe_profile"},
		{"/v1beta/models/gemini-highest:generateContent", `{"contents":[],"generationConfig":{"response_modalities":["IMAGE"]}}`, "16", "", "automatic_probe_unsafe_profile"},
		{"/v1beta/models/gemini-highest:generateContent", `{"contents":[],"generationConfig":{"responseModalities":["AUDIO"],"speechConfig":{"voiceConfig":{}}}}`, "16", "", "automatic_probe_unsafe_profile"},
		{"/v1beta/models/gemini-highest:generateContent", `{"contents":[],"generationConfig":{"speechConfig":{"voiceConfig":{}}}}`, "16", "", "automatic_probe_unsafe_profile"},
		{"/v1beta/models/gemini-highest:generateContent", `{"contents":[],"generationConfig":{"speech_config":{"voice_config":{}}}}`, "16", "", "automatic_probe_unsafe_profile"},
		{"/v1/chat/completions", `{"model":"gpt-highest","n":2}`, "16", "", "automatic_probe_unsafe_profile"},
		{"/v1/messages", `{"model":"claude-highest","messages":[{"content":[{"type":"thinking","signature":"signed"}]}]}`, "16", "", "automatic_probe_unsafe_profile"},
		{"/v1/chat/completions", `{"model":"gpt-highest","max_tokens":1,"max_tokens":9999}`, "16", "", "automatic_probe_unsafe_payload"},
		{"/v1/chat/completions", `{"model":"gpt-highest","max_tokens":"9999"}`, "16", "", "automatic_probe_unsafe_payload"},
		{"/v1/chat/completions", `{bad`, "16", "", "automatic_probe_unsafe_payload"},
		{"/v1/chat/completions", `{}`, "16", "gzip", "automatic_probe_unsafe_profile"},
		{"/v1/images/generations", `{}`, "16", "", "automatic_probe_unsupported_protocol"},
		{"/v1/chat/completions", `{}`, "128", "", "automatic_probe_output_hint_invalid"},
	} {
		t.Run(fixture.code+fixture.body, func(t *testing.T) {
			var posts atomic.Int64
			origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { posts.Add(1) }))
			defer origin.Close()
			client := NewProxyAwareHTTPClient(cpa2sub2apiAutomaticContext(fixture.hint), &config.Config{}, &cliproxyauth.Auth{Prefix:cpa2sub2apiTestPrefix}, time.Second)
			defer client.CloseIdleConnections()
			request, _ := http.NewRequest(http.MethodPost, origin.URL+fixture.path, strings.NewReader(fixture.body))
			request.Header.Set("Content-Encoding", fixture.encoding)
			response, err := client.Do(request)
			if err != nil { t.Fatal(err) }
			data, _ := io.ReadAll(response.Body)
			_ = response.Body.Close()
			var result struct { Error struct { Code, Message string } }
			_ = json.Unmarshal(data, &result)
			if response.StatusCode != 400 || result.Error.Code != fixture.code || result.Error.Message != fixture.code || posts.Load() != 0 { t.Fatalf("unsafe request status=%d body=%s posts=%d", response.StatusCode, data, posts.Load()) }
		})
	}
}

func TestCPA2Sub2APIDiagnosticAutomaticHintCannotChangeManualOrBusinessPayload(t *testing.T) {
	cpa2sub2apiSetAllowed(t, cpa2sub2apiTestPrefix)
	for _, fixture := range []struct { prefix, hint string }{
		{cpa2sub2apiTestPrefix, "absent"}, {"r"+strings.Repeat("a",40), "16"}, {"p"+strings.Repeat("a",40), "16"},
	} {
		origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			data, _ := io.ReadAll(r.Body)
			if string(data) != `{"max_tokens":1024}` { t.Errorf("manual/business payload changed: %s", data) }
		}))
		client := NewProxyAwareHTTPClient(cpa2sub2apiAutomaticContext(fixture.hint), &config.Config{}, &cliproxyauth.Auth{Prefix:fixture.prefix}, time.Second)
		request, _ := http.NewRequest(http.MethodPost, origin.URL+"/v1/messages", strings.NewReader(`{"max_tokens":1024}`))
		request.Header.Set("X-CPA2SUB2API-Auto-Output-Cap", "16")
		response, err := client.Do(request)
		if err != nil { t.Fatal(err) }
		_ = response.Body.Close()
		client.CloseIdleConnections()
		origin.Close()
	}
}

func TestCPA2Sub2APIDiagnosticMultimodalManualAndBusinessAreUnchanged(t *testing.T) {
	cpa2sub2apiSetAllowed(t, cpa2sub2apiTestPrefix)
	for _, fixture := range []struct { prefix, hint string }{
		{cpa2sub2apiTestPrefix, "absent"}, {"r"+strings.Repeat("a",40), "16"}, {"p"+strings.Repeat("a",40), "16"},
	} {
		body := `{"contents":[],"generationConfig":{"responseModalities":["AUDIO"],"speechConfig":{"voiceConfig":{}}}}`
		origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			data, _ := io.ReadAll(r.Body)
			if string(data) != body { t.Errorf("manual/business multimodal payload changed: %s", data) }
		}))
		client := NewProxyAwareHTTPClient(cpa2sub2apiAutomaticContext(fixture.hint), &config.Config{}, &cliproxyauth.Auth{Prefix:fixture.prefix}, time.Second)
		request, _ := http.NewRequest(http.MethodPost, origin.URL+"/v1beta/models/gemini-highest:generateContent", strings.NewReader(body))
		response, err := client.Do(request)
		if err != nil { t.Fatal(err) }
		_ = response.Body.Close()
		client.CloseIdleConnections()
		origin.Close()
	}
}
