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
		{name: "request empty", exchange: validExchange(), contains: "must not be empty"},
		{name: "request repeats identity", exchange: validExchange(), contains: "must not be repeated"},
		{name: "response error", exchange: validExchange(), contains: "project exchange 1 response"},
		{name: "response repeats identity", exchange: validExchange(), contains: "must not be repeated"},
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
			}
			_, err := DigestExchanges([]Exchange{exchange})
			require.ErrorContains(t, err, test.contains)
		})
	}
}

func validExchange() Exchange {
	return Exchange{
		Provider: "slack", Operation: "chat.postMessage",
		Request:  requestFact{value: map[string]any{"content_digest": digest("1")}},
		Response: responseFact{observed: true, value: map[string]any{"content_digest": digest("2")}},
	}
}

func digest(character string) string {
	result := ""
	for range 64 {
		result += character
	}
	return result
}
