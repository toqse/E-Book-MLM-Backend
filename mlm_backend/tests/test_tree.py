import pytest

from apps.mlm_tree.services import BinaryTreeService
from apps.users.models import User
from apps.users.services import allocate_member_identity


@pytest.mark.django_db
def test_bfs_placement(system_config):
    mid1, r1, l1 = allocate_member_identity()
    s = User(phone="9000000001", full_name="S", member_id=mid1, referral_code=r1, referral_link=l1)
    s.set_unusable_password()
    s.save()
    s.is_member = True
    s.save()
    BinaryTreeService.place_member(s, None)

    mid2, r2, l2 = allocate_member_identity()
    u = User(
        phone="9000000002",
        full_name="U",
        member_id=mid2,
        referral_code=r2,
        referral_link=l2,
        sponsor=s,
    )
    u.set_unusable_password()
    u.save()
    u.is_member = True
    u.save()
    n = BinaryTreeService.place_member(u, s)
    assert n.parent_id == s.binary_node.id
    assert n.position in ("LEFT", "RIGHT")
