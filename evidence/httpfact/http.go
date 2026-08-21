package httpfact

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"unicode/utf8"
)

// Request is a detached value snapshot of one HTTP request.
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
	if len(digest) != sha256.Size {
		return Request{}, fmt.Errorf("http request content digest must be SHA-256")
	}
	request := Request{
		Method:        method,
		TargetClass:   targetClass,
		BodyLength:    bodyLength,
		ContentDigest: hex.EncodeToString(digest),
	}
	if err := validateRequest(request); err != nil {
		return Request{}, err
	}
	return request, nil
}

// RequestEvidence returns the minimized request object used as JSON-DIGEST input.
func (request Request) RequestEvidence() (map[string]any, error) {
	if err := validateRequest(request); err != nil {
		return nil, err
	}
	return map[string]any{
		"method":         request.Method,
		"target_class":   request.TargetClass,
		"body_length":    json.Number(strconv.FormatInt(request.BodyLength, 10)),
		"content_digest": request.ContentDigest,
	}, nil
}

// Response is a detached value snapshot of one observed HTTP response.
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
	if len(digest) != sha256.Size {
		return Response{}, fmt.Errorf("http response content digest must be SHA-256")
	}
	response := Response{
		StatusCode:    statusCode,
		ProviderCode:  providerCode,
		MediaType:     mediaType,
		BodyLength:    bodyLength,
		ContentDigest: hex.EncodeToString(digest),
		Accepted:      accepted,
	}
	providerCode, mediaType, err := validateResponse(response)
	if err != nil {
		return Response{}, err
	}
	response.ProviderCode = providerCode
	response.MediaType = mediaType
	return response, nil
}

// ResponseEvidence returns the minimized response object used as JSON-DIGEST input.
func (response Response) ResponseEvidence() (map[string]any, error) {
	providerCode, mediaType, err := validateResponse(response)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"status_code":    json.Number(strconv.Itoa(response.StatusCode)),
		"provider_code":  providerCode,
		"media_type":     mediaType,
		"body_length":    json.Number(strconv.FormatInt(response.BodyLength, 10)),
		"content_digest": response.ContentDigest,
		"accepted":       response.Accepted,
	}, nil
}

// ResponseObserved reports true because Response represents a received HTTP response.
func (Response) ResponseObserved() bool { return true }

// NormalizeProviderCode returns the provider-code representation used in
// response evidence. Applications that substitute hostile provider values can
// call it before constructing a fact without copying these rules.
func NormalizeProviderCode(code string) (string, error) {
	code = strings.TrimSpace(code)
	if len(code) > 64 {
		return "", fmt.Errorf("http response provider code must not exceed 64 bytes")
	}
	for _, character := range code {
		if !(character == '_' || character == '-' || character == '.' || character >= 'a' && character <= 'z' || character >= 'A' && character <= 'Z' || character >= '0' && character <= '9') {
			return "", fmt.Errorf("http response provider code contains an unsupported character")
		}
	}
	return code, nil
}

// NormalizeMediaType returns the media-type representation used in response
// evidence, without parameters.
func NormalizeMediaType(mediaType string) (string, error) {
	if !utf8.ValidString(mediaType) {
		return "", fmt.Errorf("http response media type must be valid UTF-8")
	}
	return strings.TrimSpace(strings.Split(mediaType, ";")[0]), nil
}

func validateRequest(request Request) error {
	if strings.TrimSpace(request.Method) == "" {
		return fmt.Errorf("http method is required")
	}
	if !utf8.ValidString(request.Method) || !utf8.ValidString(request.TargetClass) {
		return fmt.Errorf("http request metadata must be valid UTF-8")
	}
	if request.BodyLength < 0 {
		return fmt.Errorf("http request body length must not be negative")
	}
	if !validSHA256Hex(request.ContentDigest) {
		return fmt.Errorf("http request content digest must be lowercase hexadecimal SHA-256")
	}
	return nil
}

func validateResponse(response Response) (string, string, error) {
	if response.StatusCode < 100 || response.StatusCode > 599 {
		return "", "", fmt.Errorf("http response status code must be between 100 and 599")
	}
	if response.BodyLength < 0 {
		return "", "", fmt.Errorf("http response body length must not be negative")
	}
	if !validSHA256Hex(response.ContentDigest) {
		return "", "", fmt.Errorf("http response content digest must be lowercase hexadecimal SHA-256")
	}
	providerCode, err := NormalizeProviderCode(response.ProviderCode)
	if err != nil {
		return "", "", err
	}
	mediaType, err := NormalizeMediaType(response.MediaType)
	if err != nil {
		return "", "", err
	}
	return providerCode, mediaType, nil
}

func validSHA256Hex(value string) bool {
	if len(value) != sha256.Size*2 || strings.ToLower(value) != value {
		return false
	}
	decoded, err := hex.DecodeString(value)
	return err == nil && len(decoded) == sha256.Size
}
