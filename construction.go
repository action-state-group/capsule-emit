package producer

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
)

const (
	digestAlgorithmSHA256 = "SHA-256"
	foreignArtifactType   = "foreign-artifact"
	capsuleReferenceType  = "capsule"
)

// Carry builds a Capsule that binds an opaque foreign artifact exactly as
// received, without reinterpreting or canonicalizing its bytes.
func Carry(input Input, artifact []byte) (BuiltPayload, error) {
	return Received(input, artifact, foreignArtifactType)
}

// Received builds a Capsule that binds an opaque foreign artifact and its
// caller-declared registered CPB type.
func Received(input Input, artifact []byte, artifactType string) (BuiltPayload, error) {
	if len(artifact) == 0 {
		return BuiltPayload{}, fmt.Errorf("received artifact must not be empty")
	}
	if strings.TrimSpace(artifactType) == "" {
		return BuiltPayload{}, fmt.Errorf("received artifact type must be a non-empty string")
	}
	digest := sha256.Sum256(artifact)
	digestText := hex.EncodeToString(digest[:])
	input.compute = &computeAttestation{
		CarriedArtifact: &digestReference{
			Type:      artifactType,
			DigestAlg: digestAlgorithmSHA256,
			Digest:    digestText,
		},
		CarriedInputDigest: digestText,
	}
	return Build(input)
}

// Compose builds a Capsule that binds existing signature-free Capsules by ID.
// It asserts no new facts about their contents or signers.
func Compose(input Input, members []BuiltPayload) (BuiltPayload, error) {
	if len(members) == 0 {
		return BuiltPayload{}, fmt.Errorf("compose requires at least one member")
	}
	references := make([]digestReference, 0, len(members))
	seen := make(map[string]bool, len(members))
	for index, member := range members {
		if !hexDigestPattern.MatchString(member.CapsuleID) {
			return BuiltPayload{}, fmt.Errorf("compose member %d has malformed Capsule ID", index)
		}
		verified, err := VerifyCapsule(member.JSON)
		if err != nil || verified.CapsuleID == nil || *verified.CapsuleID != member.CapsuleID {
			return BuiltPayload{}, fmt.Errorf("compose member %d is not a matching verified format-4 Capsule", index)
		}
		if seen[member.CapsuleID] {
			return BuiltPayload{}, fmt.Errorf("compose member %d duplicates Capsule ID %s", index, member.CapsuleID)
		}
		seen[member.CapsuleID] = true
		references = append(references, digestReference{
			Type:      capsuleReferenceType,
			DigestAlg: digestAlgorithmSHA256,
			Digest:    member.CapsuleID,
		})
	}
	input.compute = &computeAttestation{ComposedMembers: references}
	return Build(input)
}
