package evidence

import (
	"encoding/json"
	"errors"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

type requestFact struct {
	value map[string]any
	err   error
}

func (fact requestFact) RequestEvidence() (map[string]any, error) {
	return fact.value, fact.err
}

type responseFact struct {
	value    map[string]any
	observed bool
	err      error
}

func (fact responseFact) ResponseEvidence() (map[string]any, error) {
	return fact.value, fact.err
}

func (fact responseFact) ResponseObserved() bool { return fact.observed }

func TestDigestExchangesBindsOrderedRequestAndResponseFacts(t *testing.T) {
	exchanges := []Exchange{
		{
			Provider: "slack", Operation: "chat.postMessage",
			Request: requestFact{value: map[string]any{
				"method": "POST", "body_length": json.Number("48"), "content_digest": digest("1"),
			}},
			Response: responseFact{observed: true, value: map[string]any{
				"status_code": json.Number("200"), "accepted": true, "content_digest": digest("2"),
			}},
		},
		{
			Provider: "storage", Operation: "objects.commit",
			Request: requestFact{value: map[string]any{
				"method": "PUT", "body_length": json.Number("12"), "content_digest": digest("3"),
			}},
			Response: responseFact{observed: true, value: map[string]any{
				"status_code": json.Number("201"), "accepted": true, "content_digest": digest("4"),
			}},
		},
	}

	first, err := DigestExchanges(exchanges)
	require.NoError(t, err)
	second, err := DigestExchanges(exchanges)
	require.NoError(t, err)
	assert.Equal(t, first, second)
	assert.True(t, first.ResponseKnown)
	assert.Len(t, first.RequestDigest, 64)
	assert.Len(t, first.ResponseDigest, 64)

	reversed, err := DigestExchanges([]Exchange{exchanges[1], exchanges[0]})
	require.NoError(t, err)
	assert.NotEqual(t, first.RequestDigest, reversed.RequestDigest)
	assert.NotEqual(t, first.ResponseDigest, reversed.ResponseDigest)
}

func TestDigestExchangesOmitsResponseDigestWhenAnyResponseIsUnknown(t *testing.T) {
	result, err := DigestExchanges([]Exchange{{
		Provider: "slack", Operation: "chat.postMessage",
		Request:  requestFact{value: map[string]any{"content_digest": digest("1")}},
		Response: NoResponse{CauseCategory: "timeout", CauseCode: "deadline_exceeded"},
	}})
	require.NoError(t, err)
	assert.Len(t, result.RequestDigest, 64)
	assert.Empty(t, result.ResponseDigest)
	assert.False(t, result.ResponseKnown)

	evidence, err := (NoResponse{CauseCategory: "timeout", CauseCode: "deadline_exceeded"}).ResponseEvidence()
	require.NoError(t, err)
	assert.Equal(t, true, evidence["no_response"])
}

func TestDigestExchangesRejectsInvalidFacts(t *testing.T) {
	typedNil := (*requestFact)(nil)
	tests := []struct {
		name     string
		exchange Exchange
		contains string
	}{
		{name: "no exchanges", contains: "at least one"},
		{name: "provider", exchange: validExchange(), contains: "provider"},
		{name: "operation", exchange: validExchange(), contains: "operation"},
		{name: "request nil", exchange: validExchange(), contains: "request fact"},
		{name: "request typed nil", exchange: validExchange(), contains: "request fact"},
		{name: "request error", exchange: validExchange(), contains: "project exchange 1 request"},
		{name: "request empty", exchange: validExchange(), contains: "$ must not be empty"},
		{name: "request repeats identity", exchange: validExchange(), contains: "must not be repeated"},
		{name: "response error", exchange: validExchange(), contains: "project exchange 1 response"},
		{name: "response repeats identity", exchange: validExchange(), contains: "must not be repeated"},
		{name: "provider UTF-8", exchange: validExchange(), contains: "provider must be valid UTF-8"},
		{name: "request evidence UTF-8", exchange: validExchange(), contains: "must be valid UTF-8"},
		{name: "cyclic evidence", exchange: validExchange(), contains: "self exceeds maximum depth"},
		{name: "invalid evidence number", exchange: validExchange(), contains: "canonical JSON integer"},
		{name: "unsupported evidence value", exchange: validExchange(), contains: "$.count: int"},
		{name: "unsupported nested evidence value", exchange: validExchange(), contains: "$.items[0]: []string"},
		{name: "nil evidence value", exchange: validExchange(), contains: "$.optional must not be nil"},
		{name: "empty evidence array", exchange: validExchange(), contains: "$.items must not be empty"},
		{name: "empty evidence object", exchange: validExchange(), contains: "$.details must not be empty"},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			exchange := test.exchange
			switch test.name {
			case "no exchanges":
				_, err := DigestExchanges(nil)
				require.ErrorContains(t, err, test.contains)
				return
			case "provider":
				exchange.Provider = ""
			case "operation":
				exchange.Operation = ""
			case "request nil":
				exchange.Request = nil
			case "request typed nil":
				exchange.Request = typedNil
			case "request error":
				exchange.Request = requestFact{err: errors.New("broken")}
			case "request empty":
				exchange.Request = requestFact{value: map[string]any{}}
			case "request repeats identity":
				exchange.Request = requestFact{value: map[string]any{"provider": "duplicate"}}
			case "response error":
				exchange.Response = responseFact{observed: true, err: errors.New("broken")}
			case "response repeats identity":
				exchange.Response = responseFact{observed: true, value: map[string]any{"sequence": json.Number("1")}}
			case "provider UTF-8":
				exchange.Provider = string([]byte{'s', 0xff})
			case "request evidence UTF-8":
				exchange.Request = requestFact{value: map[string]any{"nested": []any{string([]byte{'x', 0xff})}}}
			case "cyclic evidence":
				cycle := map[string]any{}
				cycle["self"] = cycle
				exchange.Request = requestFact{value: cycle}
			case "invalid evidence number":
				exchange.Request = requestFact{value: map[string]any{"body_length": json.Number(`48,"provider":"forged"`)}}
			case "unsupported evidence value":
				exchange.Request = requestFact{value: map[string]any{"count": 1}}
			case "unsupported nested evidence value":
				exchange.Request = requestFact{value: map[string]any{"items": []any{[]string{"x"}}}}
			case "nil evidence value":
				exchange.Request = requestFact{value: map[string]any{"optional": nil}}
			case "empty evidence array":
				exchange.Request = requestFact{value: map[string]any{"items": []any{}}}
			case "empty evidence object":
				exchange.Request = requestFact{value: map[string]any{"details": map[string]any{}}}
			}
			_, err := DigestExchanges([]Exchange{exchange})
			require.ErrorContains(t, err, test.contains)
		})
	}
}

func TestValidateExchangeIdentity(t *testing.T) {
	require.NoError(t, ValidateExchangeIdentity("slack", "chat.postMessage"))
	require.ErrorContains(t, ValidateExchangeIdentity(" ", "chat.postMessage"), "provider")
	require.ErrorContains(t, ValidateExchangeIdentity("slack", " "), "operation")
	require.ErrorContains(t, ValidateExchangeIdentity("slack\xff", "chat.postMessage"), "UTF-8")
	require.ErrorContains(t, ValidateExchangeIdentity("slack", "chat.postMessage\xff"), "UTF-8")
}

func validExchange() Exchange {
	return Exchange{
		Provider: "slack", Operation: "chat.postMessage",
		Request:  requestFact{value: map[string]any{"content_digest": digest("1")}},
		Response: responseFact{observed: true, value: map[string]any{"content_digest": digest("2")}},
	}
}

func TestDigestExchangesRejectsNonCanonicalNumbers(t *testing.T) {
	for _, value := range []string{"", "N/A", "007", "+5", "0x10", "-", "-0", "1.0", "1e2", `48,"buf":"abab"`} {
		t.Run(value, func(t *testing.T) {
			exchange := validExchange()
			exchange.Request = requestFact{value: map[string]any{"value": json.Number(value)}}
			_, err := DigestExchanges([]Exchange{exchange})
			require.ErrorContains(t, err, "canonical JSON integer")
		})
	}

	for _, value := range []string{"0", "1", "-1", "9007199254740991", "-9007199254740991"} {
		t.Run("valid/"+value, func(t *testing.T) {
			exchange := validExchange()
			exchange.Request = requestFact{value: map[string]any{"value": json.Number(value)}}
			_, err := DigestExchanges([]Exchange{exchange})
			require.NoError(t, err)
		})
	}
}

func digest(character string) string {
	result := ""
	for range 64 {
		result += character
	}
	return result
}
