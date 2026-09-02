package emit

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
	slotWho               = "who"
	slotCan               = "can"
	slotDid               = "did"
	slotAudit             = "audit"
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

// Who assigns an existing Capsule to the identity or pedigree slot.
func Who(member BuiltPayload) SlotMember {
	return SlotMember{slot: slotWho, member: member}
}

// Can assigns an existing Capsule to the authority or mandate slot.
func Can(member BuiltPayload) SlotMember {
	return SlotMember{slot: slotCan, member: member}
}

// Did assigns an existing Capsule to the performed-action slot.
func Did(member BuiltPayload) SlotMember {
	return SlotMember{slot: slotDid, member: member}
}

// Audit assigns an existing Capsule to the independent-review slot.
func Audit(member BuiltPayload) SlotMember {
	return SlotMember{slot: slotAudit, member: member}
}

// BuildComposition builds a Capsule that binds existing signature-free
// Capsules into WHO, CAN, DID, and AUDIT roles. It emits members in canonical
// slot order regardless of argument order and asserts no new facts about the
// members' contents or signers.
func BuildComposition(input Input, members ...SlotMember) (BuiltPayload, error) {
	if len(members) == 0 {
		return BuiltPayload{}, fmt.Errorf("composition requires at least one slot member")
	}
	bySlot := make(map[string]BuiltPayload, len(members))
	seenIDs := make(map[string]string, len(members))
	for index, slotted := range members {
		if !isCompositionSlot(slotted.slot) {
			return BuiltPayload{}, fmt.Errorf("composition member %d has invalid slot", index)
		}
		if _, exists := bySlot[slotted.slot]; exists {
			return BuiltPayload{}, fmt.Errorf("composition duplicates %s slot", slotted.slot)
		}
		member := slotted.member
		if !hexDigestPattern.MatchString(member.CapsuleID) {
			return BuiltPayload{}, fmt.Errorf("composition %s member has malformed Capsule ID", slotted.slot)
		}
		verified, err := VerifyCapsule(member.JSON)
		if err != nil || verified.CapsuleID == nil || *verified.CapsuleID != member.CapsuleID {
			return BuiltPayload{}, fmt.Errorf("composition %s member is not a matching verified format-4 Capsule", slotted.slot)
		}
		if priorSlot, exists := seenIDs[member.CapsuleID]; exists {
			return BuiltPayload{}, fmt.Errorf("composition %s slot duplicates Capsule ID %s from %s slot", slotted.slot, member.CapsuleID, priorSlot)
		}
		seenIDs[member.CapsuleID] = slotted.slot
		bySlot[slotted.slot] = member
	}

	references := make([]digestReference, 0, len(members))
	for _, slot := range compositionSlots() {
		member, exists := bySlot[slot]
		if !exists {
			continue
		}
		references = append(references, digestReference{
			Type:      capsuleReferenceType,
			DigestAlg: digestAlgorithmSHA256,
			Digest:    member.CapsuleID,
			Slot:      slot,
		})
	}
	input.compute = &computeAttestation{ComposedMembers: references}
	return Build(input)
}

func isCompositionSlot(slot string) bool {
	for _, candidate := range compositionSlots() {
		if slot == candidate {
			return true
		}
	}
	return false
}

func compositionSlots() [4]string {
	return [...]string{slotWho, slotCan, slotDid, slotAudit}
}
