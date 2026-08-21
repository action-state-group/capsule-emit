package httpfact

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
)

// Request is an immutable evidence snapshot of one HTTP request.
type Request struct {
	Method        string
	TargetClass   string
	BodyLength    int64
	ContentDigest string
}

// CaptureRequest snapshots request metadata and hashes the supplied body bytes.
// The caller supplies the exact bytes sent on the wire after any encoding.
func CaptureRequest(request *http.Request, body []byte, targetClass string) (Request, error) {
	if request == nil {
		return Request{}, fmt.Errorf("http request is required")
	}
	digest := sha256.Sum256(body)
	return NewRequestWithDigest(request.Method, targetClass, int64(len(body)), digest[:])
}

// NewRequestWithDigest builds a snapshot for streamed content whose digest was
// computed while sending the body.
func NewRequestWithDigest(method, targetClass string, bodyLength int64, digest []byte) (Request, error) {
	if strings.TrimSpace(method) == "" {
		return Request{}, fmt.Errorf("http method is required")
	}
	if bodyLength < 0 {
		return Request{}, fmt.Errorf("http request body length must not be negative")
	}
	if len(digest) != sha256.Size {
		return Request{}, fmt.Errorf("http request content digest must be SHA-256")
	}
	return Request{
		Method:        method,
		TargetClass:   targetClass,
		BodyLength:    bodyLength,
		ContentDigest: hex.EncodeToString(digest),
	}, nil
}

// RequestEvidence returns the minimized request object used as JSON-DIGEST input.
func (request Request) RequestEvidence() (map[string]any, error) {
	return map[string]any{
		"method":         request.Method,
		"target_class":   request.TargetClass,
		"body_length":    json.Number(strconv.FormatInt(request.BodyLength, 10)),
		"content_digest": request.ContentDigest,
	}, nil
}

// Response is an immutable evidence snapshot of one observed HTTP response.
type Response struct {
	StatusCode    int
	ProviderCode  string
	MediaType     string
	BodyLength    int64
	ContentDigest string
	Accepted      bool
}

// CaptureResponse snapshots response metadata and hashes the supplied body bytes.
func CaptureResponse(response *http.Response, body []byte, providerCode string, accepted bool) (Response, error) {
	if response == nil {
		return Response{}, fmt.Errorf("http response is required")
	}
	digest := sha256.Sum256(body)
	return NewResponseWithDigest(response.StatusCode, providerCode, response.Header.Get("Content-Type"), int64(len(body)), digest[:], accepted)
}

// NewResponseWithDigest builds a snapshot for a streamed response body.
func NewResponseWithDigest(statusCode int, providerCode, mediaType string, bodyLength int64, digest []byte, accepted bool) (Response, error) {
	if statusCode < 100 || statusCode > 599 {
		return Response{}, fmt.Errorf("http response status code must be between 100 and 599")
	}
	if bodyLength < 0 {
		return Response{}, fmt.Errorf("http response body length must not be negative")
	}
	if len(digest) != sha256.Size {
		return Response{}, fmt.Errorf("http response content digest must be SHA-256")
	}
	return Response{
		StatusCode:    statusCode,
		ProviderCode:  safeCode(providerCode),
		MediaType:     strings.TrimSpace(strings.Split(mediaType, ";")[0]),
		BodyLength:    bodyLength,
		ContentDigest: hex.EncodeToString(digest),
		Accepted:      accepted,
	}, nil
}

// ResponseEvidence returns the minimized response object used as JSON-DIGEST input.
func (response Response) ResponseEvidence() (map[string]any, error) {
	return map[string]any{
		"status_code":    json.Number(strconv.Itoa(response.StatusCode)),
		"provider_code":  response.ProviderCode,
		"media_type":     response.MediaType,
		"body_length":    json.Number(strconv.FormatInt(response.BodyLength, 10)),
		"content_digest": response.ContentDigest,
		"accepted":       response.Accepted,
	}, nil
}

// ResponseObserved reports true because Response represents a received HTTP response.
func (Response) ResponseObserved() bool { return true }

func safeCode(code string) string {
	code = strings.TrimSpace(code)
	if len(code) > 64 {
		return "provider_error"
	}
	for _, character := range code {
		if !(character == '_' || character == '-' || character == '.' || character >= 'a' && character <= 'z' || character >= 'A' && character <= 'Z' || character >= '0' && character <= '9') {
			return "provider_error"
		}
	}
	return code
}
