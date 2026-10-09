package emit

import (
	"encoding/json"
	"fmt"
	"regexp"

	"github.com/action-state-group/agent-action-capsule/go/canonical"
)

var extensionNamePattern = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)

// baseMembers are the top-level member names the base profile defines,
// including the ones this library never writes and the local-only envelope
// fields that capsule_id derivation strips. An Extension may not use them.
var baseMembers = map[string]bool{
	"spec_version": true, "format_version": true, "canonicalization_id": true,
	"capsule_id": true, "action_id": true, "action_type": true, "operator": true,
	"developer": true, "timestamp": true, "epoch_id": true, "domain": true,
	"provenance": true, "provenance_mode": true, "disposition": true,
	"effect": true, "chain": true, "assurance": true, "cross_party": true,
	"model_attestation": true, "references": true, "constraints": true,
	"compliance": true, "signature": true, "key_id": true,
}

// extensionValue validates one Extension against the payload built so far and
// returns its decoded value.
func extensionValue(extension Extension, payload map[string]any) (any, error) {
	if !extensionNamePattern.MatchString(extension.Name) {
		return nil, fmt.Errorf("extension name %q must be lowercase letters, digits and underscores, starting with a letter", extension.Name)
	}
	if baseMembers[extension.Name] {
		return nil, fmt.Errorf("extension %q is a base-profile member", extension.Name)
	}
	if _, exists := payload[extension.Name]; exists {
		return nil, fmt.Errorf("extension %q is given more than once", extension.Name)
	}
	if len(extension.Value) == 0 {
		return nil, fmt.Errorf("extension %q has no value", extension.Name)
	}
	value, err := decodeStrictJSON(extension.Value)
	if err != nil {
		return nil, fmt.Errorf("extension %q: %w", extension.Name, err)
	}
	if value == nil {
		return nil, fmt.Errorf("extension %q must not be null", extension.Name)
	}
	if err := checkExtensionNumbers(value, "$."+extension.Name); err != nil {
		return nil, err
	}
	return value, nil
}

// checkExtensionNumbers refuses a float or an integer outside the
// interoperable range anywhere in an extension value (base profile §5.1).
func checkExtensionNumbers(value any, path string) error {
	switch typed := value.(type) {
	case json.Number:
		if canonical.IsFloat(typed) {
			return fmt.Errorf("float at %s: floating-point values are forbidden in a Capsule", path)
		}
		if canonical.IsUnsafeInt(typed) {
			return fmt.Errorf("integer at %s is outside the interoperable range", path)
		}
	case map[string]any:
		for key, item := range typed {
			if err := checkExtensionNumbers(item, path+"."+key); err != nil {
				return err
			}
		}
	case []any:
		for index, item := range typed {
			if err := checkExtensionNumbers(item, fmt.Sprintf("%s[%d]", path, index)); err != nil {
				return err
			}
		}
	}
	return nil
}
