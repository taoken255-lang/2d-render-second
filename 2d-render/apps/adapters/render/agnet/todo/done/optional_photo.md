full picture:
Avatar IDs: Agnet-internal (ID → storage folder mapping in Agnet service)
Future-proofing: Unknown which adapters will support preset assets
Precedence: If both image + avatar_id provided → use avatar_id, warn about ignored image

## Reference

**Agnet Service Input Methods**: [/home/igor/repos/2d-render/docs/input.md](../../2d-render/docs/input.md)

The Agnet gRPC service supports two input methods:
1. **Direct Image Input** - Client sends photo bytes (current flow)
2. **Avatar ID Input** - Client sends avatar ID, service loads pre-recorded video from `/app/assets/{avatar_id}/`

This change enables Avatar ID method by making photo optional when `params.avatar_id` is provided.

---

Final Recommendation: Option B (Adapter-Level Validation)
This is clearly the right choice because:
✅ Future-proof: Any adapter can implement preset assets without API changes
✅ Loose coupling: API doesn't need to know which engines support what
✅ Adapter autonomy: Each adapter validates its own requirements
✅ Extensible pattern: Preset assets could become common (avatars, backgrounds, templates)
Implementation Plan
1. API Layer - Make photo optional
File: apps/producer/api.py
Line 195: Change photo_s3: str = Field(...) → photo_s3: Optional[str] = Field(None, ...)
Lines 221-226: Update validation to allow None if photo_s3 not provided
Line 323-326: Update bucket validation to skip if photo_s3 is None
2. Contract Layer - Make photo optional
File: apps/common/contracts.py
Line 43: Change photo_s3: str → photo_s3: Optional[str] = None
3. Adapter Base Model - Make photo optional
File: apps/common/adapter/base_models.py
Line 29: Change photo_url: str → photo_url: Optional[str] = None
4. Omni Adapter - Add validation (photo required)
File: apps/omni_adapter/app.py
Add validation at start of /render/start endpoint (~line 72):
if not request.inputs.photo_url:
    raise ValueError("photo_url is required for Omni engine")
5. Agnet Adapter - Add validation (photo OR avatar_id)
File: apps/agnet_adapter/app.py
Add validation at start of /render/start endpoint (~line 70):
avatar_id = request.params.get("avatar_id")
photo_url = request.inputs.photo_url

if not photo_url and not avatar_id:
    raise ValueError("Either photo_url or params.avatar_id required for Agnet")

if photo_url and avatar_id:
    logger.warning(f"Both photo_url and avatar_id provided. Using avatar_id={avatar_id}, ignoring photo_url")
6. Agnet Adapter Runner - Use avatar_id from params
File: apps/agnet_adapter/runner.py
Line 152-157: Modify image download logic to skip if avatar_id provided
Line 330: Change avatar_id="default" → use from params or default:
avatar_id = self.job_request.params.get("avatar_id", "default")
Lines 299-308: Modify _preprocess_image to handle None photo_url (skip download if avatar_id)
7. Update Tests
Add test cases for Agnet with avatar_id (no photo)
Add test case for Omni without photo (should fail)
Add test case for Agnet with both (verify warning logged)
Benefits of This Approach
No API coupling: API doesn't know/care about engine-specific features
Self-documenting: Each adapter's code shows its requirements
Easy to extend: Future adapters (backgrounds, templates, voices) follow same pattern
Backward compatible: Existing requests with photos continue working
Fail-fast where it matters: Adapters validate immediately on /render/start
Example Usage After Changes
# Current way (still works)
POST /render?engine=agnet
{
  "inputs": {"audio_s3": "s3://...", "photo_s3": "s3://..."},
  "params": {}
}

# New way (avatar_id)
POST /render?engine=agnet
{
  "inputs": {"audio_s3": "s3://..."},
  "params": {"avatar_id": "corporate-woman-01"}
}

# Omni (still requires photo)
POST /render?engine=omni
{
  "inputs": {"audio_s3": "s3://..."},  # ❌ Fails: photo_url required
  "params": {}
}