import pytest
from rest_framework.test import APIClient

from apps.mlm_tree.services import BinaryTreeService
from apps.users.models import User
from apps.users.services import allocate_member_identity


def _max_nested_depth(node: dict | None) -> int:
    """Depth of nested left/right below this node (0 = leaf)."""
    if node is None:
        return -1
    d = 0
    for k in ("left", "right"):
        ch = node.get(k)
        if ch is not None:
            d = max(d, 1 + _max_nested_depth(ch))
    return d


@pytest.mark.django_db
def test_bfs_placement(system_config):
    mid1, r1, l1 = allocate_member_identity()
    s = User(phone="+919000000001", full_name="S", member_id=mid1, referral_code=r1, referral_link=l1)
    s.set_unusable_password()
    s.save()
    s.is_member = True
    s.save()
    BinaryTreeService.place_member(s, None)

    mid2, r2, l2 = allocate_member_identity()
    u = User(
        phone="+919000000002",
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


@pytest.mark.django_db
def test_tree_subtree_self_and_child(system_config):
    mid1, r1, l1 = allocate_member_identity()
    sponsor = User(
        phone="+919000000101",
        full_name="Sponsor",
        member_id=mid1,
        referral_code=r1,
        referral_link=l1,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.is_member = True
    sponsor.save()
    BinaryTreeService.place_member(sponsor, None)

    mid2, r2, l2 = allocate_member_identity()
    child = User(
        phone="+919000000102",
        full_name="Child",
        member_id=mid2,
        referral_code=r2,
        referral_link=l2,
        sponsor=sponsor,
    )
    child.set_unusable_password()
    child.save()
    child.is_member = True
    child.save()
    BinaryTreeService.place_member(child, sponsor)

    client = APIClient()
    client.force_authenticate(user=sponsor)

    me = client.get("/api/v1/user/tree/subtree/")
    assert me.status_code == 200
    body = me.json()["data"]
    assert body["anchor_member_id"] == sponsor.member_id
    assert body["max_depth"] == 3
    assert body["root"]["member_id"] == sponsor.member_id

    sub = client.get(f"/api/v1/user/tree/subtree/?anchor_member_id={child.member_id}")
    assert sub.status_code == 200
    b2 = sub.json()["data"]
    assert b2["root"]["member_id"] == child.member_id
    assert b2["anchor_member_id"] == child.member_id


@pytest.mark.django_db
def test_tree_subtree_forbidden_outside_leg(system_config):
    mid_a, r_a, l_a = allocate_member_identity()
    a = User(
        phone="+919000000201",
        full_name="A",
        member_id=mid_a,
        referral_code=r_a,
        referral_link=l_a,
    )
    a.set_unusable_password()
    a.save()
    a.is_member = True
    a.save()
    BinaryTreeService.place_member(a, None)

    mid_b, r_b, l_b = allocate_member_identity()
    b = User(
        phone="+919000000202",
        full_name="B",
        member_id=mid_b,
        referral_code=r_b,
        referral_link=l_b,
    )
    b.set_unusable_password()
    b.save()
    b.is_member = True
    b.save()
    BinaryTreeService.place_member(b, None)

    client = APIClient()
    client.force_authenticate(user=a)
    r = client.get(f"/api/v1/user/tree/subtree/?anchor_member_id={b.member_id}")
    assert r.status_code == 403


@pytest.mark.django_db
def test_tree_subtree_anchor_not_found(system_config):
    mid1, r1, l1 = allocate_member_identity()
    sponsor = User(
        phone="+919000000301",
        full_name="S",
        member_id=mid1,
        referral_code=r1,
        referral_link=l1,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.is_member = True
    sponsor.save()
    BinaryTreeService.place_member(sponsor, None)

    client = APIClient()
    client.force_authenticate(user=sponsor)
    r = client.get("/api/v1/user/tree/subtree/?anchor_member_id=NO_SUCH_MEMBER")
    assert r.status_code == 404


@pytest.mark.django_db
def test_tree_subtree_viewer_not_placed(system_config):
    mid_s, r_s, l_s = allocate_member_identity()
    sponsor = User(
        phone="+919000000401",
        full_name="S",
        member_id=mid_s,
        referral_code=r_s,
        referral_link=l_s,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.is_member = True
    sponsor.save()
    BinaryTreeService.place_member(sponsor, None)

    mid_u, r_u, l_u = allocate_member_identity()
    u = User(
        phone="+919000000402",
        full_name="U",
        member_id=mid_u,
        referral_code=r_u,
        referral_link=l_u,
        sponsor=sponsor,
    )
    u.set_unusable_password()
    u.save()
    u.is_member = True
    u.save()

    client = APIClient()
    client.force_authenticate(user=u)
    self_r = client.get("/api/v1/user/tree/subtree/")
    assert self_r.status_code == 200
    assert self_r.json()["data"]["root"] is None

    other = client.get(f"/api/v1/user/tree/subtree/?anchor_member_id={sponsor.member_id}")
    assert other.status_code == 403


@pytest.mark.django_db
def test_tree_subtree_max_depth_cap(system_config):
    mid1, r1, l1 = allocate_member_identity()
    sponsor = User(
        phone="+919000000501",
        full_name="S",
        member_id=mid1,
        referral_code=r1,
        referral_link=l1,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.is_member = True
    sponsor.save()
    BinaryTreeService.place_member(sponsor, None)

    client = APIClient()
    client.force_authenticate(user=sponsor)
    r = client.get("/api/v1/user/tree/subtree/?max_depth=99")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["max_depth"] == 10
    # Single-node tree: nested depth below root is 0
    assert _max_nested_depth(data["root"]) == 0
