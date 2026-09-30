from uuid import UUID

from app.contracts.signing import calculate_signature


def test_hmac_signature_matches_protocol_golden_vector() -> None:
    signature = calculate_signature(
        "0123456789abcdef0123456789abcdef",
        1_700_000_000,
        UUID("6d9f6a9d-1b8c-4d5f-8c3d-8a8f2d7b4c11"),
        b'{"message":"halo"}',
    )

    assert signature == (
        "v1=1bd33d10caa535ecd3c41ece301d41c22f5e1addec36846b0d96e3f9f497fe3c"
    )
