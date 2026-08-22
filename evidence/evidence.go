package evidence

import (
	"encoding/json"
	"fmt"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/action-state-group/agent-action-capsule/go/canonical"
)

var jsonIntegerPattern = regexp.MustCompile(`^(0|[1-9][0-9]*|-[1-9][0-9]*)$`)

const (
	// EffectEvidenceSchemaV1 identifies the ordered evidence-envelope convention
	// defined by this library. It is not a field defined by the AAC specification.
	EffectEvidenceSchemaV1 = "urn:capsule-producer:effect-evidence:v1"
	maxEvidenceDepth       = 1000
)

// RequestFact returns the minimized canonical value for one outbound request.
// Values may contain bool, string, json.Number, non-empty []any, and non-empty
// map[string]any. Numbers must be canonical JSON integers represented as
// json.Number. Nil and empty containers are rejected uniformly because AAC
// normalization removes them when they are object members; allowing them only
// in array positions would make their digest meaning depend on location.
type RequestFact interface {
	RequestEvidence() (map[string]any, error)
}

// ResponseFact returns the minimized canonical value for one response and says
// whether a response was actually observed. Its value contract matches RequestFact.
type ResponseFact interface {
	ResponseEvidence() (map[string]any, error)
	ResponseObserved() bool
}

// Exchange pairs one outbound request with its corresponding response.
// Provider and Operation live here so transport-specific facts do not repeat them.
type Exchange struct {
	Provider  string
	Operation string
	Request   RequestFact
	Response  ResponseFact
}

// ValidateExchangeIdentity checks the provider and operation fields shared by
// request and response evidence. Applications should call it before dispatch
// when an invalid identity would otherwise make terminal digesting fail after
// an effect has already occurred.
func ValidateExchangeIdentity(provider, operation string) error {
	if strings.TrimSpace(provider) == "" {
		return fmt.Errorf("provider is required")
	}
	if strings.TrimSpace(operation) == "" {
		return fmt.Errorf("operation is required")
	}
	if !utf8.ValidString(provider) {
		return fmt.Errorf("provider must be valid UTF-8")
	}
	if !utf8.ValidString(operation) {
		return fmt.Errorf("operation must be valid UTF-8")
	}
	return nil
}

// Digests binds the ordered request facts and, when complete, response facts.
type Digests struct {
	RequestDigest  string
	ResponseDigest string
	ResponseKnown  bool
}

// NoResponse records that no provider response was observed. Cause values are
// application diagnostics and are not included in a response digest.
type NoResponse struct {
	CauseCategory string
	CauseCode     string
}

// ResponseEvidence returns diagnostic metadata for callers that need it. The
// envelope builder does not digest it because no provider response was observed.
func (n NoResponse) ResponseEvidence() (map[string]any, error) {
	return map[string]any{
		"no_response":    true,
		"cause_category": n.CauseCategory,
		"cause_code":     n.CauseCode,
	}, nil
}

// ResponseObserved reports false because no provider response was captured.
func (NoResponse) ResponseObserved() bool { return false }

// DigestExchanges computes request_digest and, only when every response was
// observed, response_digest with the upstream AAC JSONDigest implementation.
func DigestExchanges(exchanges []Exchange) (Digests, error) {
	if len(exchanges) == 0 {
		return Digests{}, fmt.Errorf("at least one evidence exchange is required")
	}
	requests := make([]any, 0, len(exchanges))
	responses := make([]any, 0, len(exchanges))
	responseKnown := true
	for index, exchange := range exchanges {
		if err := ValidateExchangeIdentity(exchange.Provider, exchange.Operation); err != nil {
			return Digests{}, fmt.Errorf("exchange %d %w", index+1, err)
		}
		if nilInterface(exchange.Request) {
			return Digests{}, fmt.Errorf("exchange %d request fact is required", index+1)
		}
		request, err := exchange.Request.RequestEvidence()
		if err != nil {
			return Digests{}, fmt.Errorf("project exchange %d request: %w", index+1, err)
		}
		if err := validateNestedEvidence(request); err != nil {
			return Digests{}, fmt.Errorf("exchange %d request evidence: %w", index+1, err)
		}
		identity := map[string]any{
			"sequence":  json.Number(strconv.Itoa(index + 1)),
			"provider":  exchange.Provider,
			"operation": exchange.Operation,
		}
		requestEntry := cloneMap(identity)
		requestEntry["request"] = request
		requests = append(requests, requestEntry)

		if nilInterface(exchange.Response) || !exchange.Response.ResponseObserved() {
			responseKnown = false
			continue
		}
		response, err := exchange.Response.ResponseEvidence()
		if err != nil {
			return Digests{}, fmt.Errorf("project exchange %d response: %w", index+1, err)
		}
		if err := validateNestedEvidence(response); err != nil {
			return Digests{}, fmt.Errorf("exchange %d response evidence: %w", index+1, err)
		}
		responseEntry := cloneMap(identity)
		responseEntry["response"] = response
		responses = append(responses, responseEntry)
	}

	requestDigest, err := canonical.JSONDigest(envelope(requests))
	if err != nil {
		return Digests{}, fmt.Errorf("digest request evidence: %w", err)
	}
	result := Digests{RequestDigest: requestDigest, ResponseKnown: responseKnown}
	if responseKnown {
		responseDigest, err := canonical.JSONDigest(envelope(responses))
		if err != nil {
			return Digests{}, fmt.Errorf("digest response evidence: %w", err)
		}
		result.ResponseDigest = responseDigest
	}
	return result, nil
}

func envelope(exchanges []any) map[string]any {
	return map[string]any{
		"schema":    EffectEvidenceSchemaV1,
		"exchanges": exchanges,
	}
}

func validateNestedEvidence(value map[string]any) error {
	// Emptiness is enforced by validateEvidenceValue's map case below, which
	// reports the offending path; do not duplicate it with a pathless check here.
	for _, reserved := range []string{"provider", "operation", "sequence", "request", "response"} {
		if _, exists := value[reserved]; exists {
			return fmt.Errorf("reserved exchange field %q must not be repeated", reserved)
		}
	}
	return validateEvidenceValue(value, "$", 1)
}

func validateEvidenceValue(value any, path string, depth int) error {
	if depth > maxEvidenceDepth {
		return fmt.Errorf("evidence at %s exceeds maximum depth of %d", path, maxEvidenceDepth)
	}
	switch typed := value.(type) {
	case nil:
		return fmt.Errorf("evidence value at %s must not be nil", path)
	case bool:
	case string:
		if !utf8.ValidString(typed) {
			return fmt.Errorf("evidence text at %s must be valid UTF-8", path)
		}
	case json.Number:
		if !utf8.ValidString(string(typed)) {
			return fmt.Errorf("evidence number at %s must be valid UTF-8", path)
		}
		if !jsonIntegerPattern.MatchString(string(typed)) {
			return fmt.Errorf("evidence number at %s must be a canonical JSON integer", path)
		}
	case map[string]any:
		if len(typed) == 0 {
			return fmt.Errorf("evidence object at %s must not be empty", path)
		}
		for key, item := range typed {
			if !utf8.ValidString(key) {
				return fmt.Errorf("evidence object key at %s must be valid UTF-8", path)
			}
			if err := validateEvidenceValue(item, path+"."+key, depth+1); err != nil {
				return err
			}
		}
	case []any:
		if len(typed) == 0 {
			return fmt.Errorf("evidence array at %s must not be empty", path)
		}
		for index, item := range typed {
			if err := validateEvidenceValue(item, fmt.Sprintf("%s[%d]", path, index), depth+1); err != nil {
				return err
			}
		}
	default:
		return fmt.Errorf("unsupported evidence value at %s: %T; use bool, string, json.Number, non-empty []any, or non-empty map[string]any", path, value)
	}
	return nil
}

func nilInterface(value any) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	switch reflected.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return reflected.IsNil()
	default:
		return false
	}
}

func cloneMap(value map[string]any) map[string]any {
	result := make(map[string]any, len(value))
	for key, item := range value {
		result[key] = item
	}
	return result
}
