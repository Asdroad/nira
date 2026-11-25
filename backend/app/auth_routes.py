import secrets
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from .db import get_db
from .models import User, Organization, Membership, Subscription, Invitation, AuthSession, now
from .schemas import Register, Login, Invite, AcceptInvite, MemberUpdate
from .security import Actor, get_actor, hasher, verify_password, issue_session, digest, tenant_object, COOKIE_NAME
from .services import columns, audit, lock_organization
from .config import get_settings

router = APIRouter(prefix="/api/v1", tags=["Аккаунты и команда"])


def expire_invitations(db, org_id):
    for invite in db.scalars(select(Invitation).where(Invitation.organization_id == org_id, Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None), Invitation.expires_at <= now())).all():
        member = db.get(Membership, invite.membership_id)
        if member.status == "invited":
            member.status, member.slot = "expired", None
        invite.revoked_at = now()
    db.flush()


@router.post("/auth/register", status_code=201)
def register(data: Register, response: Response, db: Session = Depends(get_db)):
    email = str(data.email).lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "Этот email уже зарегистрирован")
    if data.inn and db.scalar(select(Organization.id).where(Organization.inn == data.inn)):
        raise HTTPException(409, "Кабинет с этим ИНН уже существует. Запросите приглашение у владельца")
    user = User(email=email, name=data.name, password_hash=hasher.hash(data.password))
    org = Organization(name=data.organization, inn=data.inn, type=data.organization_type, industries=data.industries)
    db.add_all([user, org]); db.flush()
    member = Membership(organization_id=org.id, user_id=user.id, email=email, role="owner", slot=0)
    db.add_all([member, Subscription(organization_id=org.id, status="development" if get_settings().env in {"local", "test"} else "awaiting_payment")])
    csrf = issue_session(db, user.id, org.id, response)
    db.flush()
    return {"user": columns(user, {"password_hash"}), "organization": columns(org), "csrf_token": csrf, "local_access": get_settings().env != "production"}


@router.post("/auth/login")
def login(data: Login, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == str(data.email).lower()))
    if not verify_password(data.password, user.password_hash if user else None) or not user or not user.active:
        raise HTTPException(401, "Неверный email или пароль")
    member = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.status == "active").order_by(Membership.created_at))
    if not member:
        raise HTTPException(403, "Нет активного доступа к организации")
    csrf = issue_session(db, user.id, member.organization_id, response)
    return {"csrf_token": csrf, "user": columns(user, {"password_hash"})}


@router.get("/auth/me")
def me(actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    memberships = db.scalars(select(Membership).where(Membership.user_id == actor.user.id, Membership.status == "active")).all()
    return {"user": columns(actor.user, {"password_hash"}), "membership": columns(actor.membership), "organization": columns(actor.organization), "csrf_token": actor.csrf, "organizations": [{"id": m.organization_id, "name": db.get(Organization, m.organization_id).name, "role": m.role} for m in memberships]}


@router.post("/auth/logout")
def logout(response: Response, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    db.delete(actor.session)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/auth/organizations/{organization_id}/switch")
def switch_org(organization_id: str, response: Response, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    member = db.scalar(select(Membership).where(Membership.user_id == actor.user.id, Membership.organization_id == organization_id, Membership.status == "active"))
    if not member:
        raise HTTPException(404, "Организация не найдена")
    db.delete(actor.session)
    return {"csrf_token": issue_session(db, actor.user.id, organization_id, response)}


@router.get("/team")
def team(actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    query = select(Membership).where(Membership.organization_id == actor.organization.id)
    if actor.membership.role != "owner" and not actor.can("view_team"):
        query = query.where(Membership.user_id == actor.user.id)
    members = db.scalars(query.order_by(Membership.slot)).all()
    occupied = db.scalars(select(Membership).where(Membership.organization_id == actor.organization.id, Membership.slot > 0, Membership.status.in_(["active", "invited"]))).all()
    actual = 0
    for m in occupied:
        if m.status == "active" or db.scalar(select(Invitation.id).where(Invitation.membership_id == m.id, Invitation.expires_at > now(), Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None))):
            actual += 1
    return {"items": [{**columns(m), "name": db.get(User, m.user_id).name if m.user_id else m.email} for m in members], "occupied": actual, "limit": 5}


@router.post("/team/invitations", status_code=201)
def invite(data: Invite, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.owner()
    lock_organization(db, actor.organization.id)
    expire_invitations(db, actor.organization.id)
    email = str(data.email).lower()
    member = db.scalar(select(Membership).where(Membership.organization_id == actor.organization.id, Membership.email == email))
    if member and member.status in {"active", "invited"}:
        raise HTTPException(409, "Участник уже добавлен или приглашён")
    slots = set(db.scalars(select(Membership.slot).where(Membership.organization_id == actor.organization.id, Membership.slot.is_not(None))).all())
    slot = next((i for i in range(1, 6) if i not in slots), None)
    if slot is None:
        raise HTTPException(409, "Все пять мест заняты или зарезервированы")
    if not member:
        member = Membership(organization_id=actor.organization.id, email=email, role=data.role, slot=slot, status="invited")
        db.add(member)
    else:
        member.role, member.slot, member.status, member.permissions = data.role, slot, "invited", {}
    db.flush()
    token = secrets.token_urlsafe(32)
    invitation = Invitation(organization_id=actor.organization.id, membership_id=member.id, token_hash=digest(token), expires_at=now() + timedelta(days=7))
    db.add(invitation); db.flush()
    audit(db, actor, "team.invited", member, "Создано приглашение сотрудника")
    # Until mail is configured the owner shares this one-time link manually.
    return {"invitation_id": invitation.id, "expires_at": invitation.expires_at, "accept_token": token, "delivery_status": "manual_link", "email_sent": False}


@router.post("/auth/accept-invitation")
def accept_invitation(data: AcceptInvite, response: Response, db: Session = Depends(get_db)):
    invitation = db.scalar(select(Invitation).where(Invitation.token_hash == digest(data.token)))
    if not invitation:
        raise HTTPException(404, "Приглашение не найдено")
    lock_organization(db, invitation.organization_id)
    db.refresh(invitation)
    member = db.get(Membership, invitation.membership_id)
    if invitation.accepted_at or invitation.revoked_at or invitation.expires_at <= now() or member.status != "invited":
        raise HTTPException(410, "Приглашение недействительно или истекло")
    user = db.scalar(select(User).where(User.email == member.email))
    if user:
        if not user.active or not verify_password(data.password, user.password_hash):
            raise HTTPException(401, "Для существующего аккаунта нужен его текущий пароль")
    else:
        user = User(name=data.name, email=member.email, password_hash=hasher.hash(data.password))
        db.add(user); db.flush()
    member.user_id, member.status = user.id, "active"
    invitation.accepted_at = now()
    return {"csrf_token": issue_session(db, user.id, member.organization_id, response), "organization_id": member.organization_id}


@router.patch("/team/{membership_id}")
def update_member(membership_id: str, data: MemberUpdate, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.owner()
    member = tenant_object(db, Membership, membership_id, actor)
    if member.role == "owner":
        raise HTTPException(409, "Роль владельца меняется отдельной процедурой передачи")
    member.role, member.permissions = data.role, data.permissions
    audit(db, actor, "team.permissions_changed", member, "Изменены права сотрудника")
    return columns(member)


@router.delete("/team/{membership_id}")
def revoke_member(membership_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.owner()
    lock_organization(db, actor.organization.id)
    member = tenant_object(db, Membership, membership_id, actor)
    if member.role == "owner":
        raise HTTPException(409, "Владельца нельзя отключить этим действием")
    member.status, member.slot = "revoked", None
    if member.user_id:
        db.execute(delete(AuthSession).where(AuthSession.user_id == member.user_id, AuthSession.organization_id == actor.organization.id))
    for invitation in db.scalars(select(Invitation).where(Invitation.membership_id == member.id, Invitation.accepted_at.is_(None))).all():
        invitation.revoked_at = now()
    audit(db, actor, "team.revoked", member, "Доступ сотрудника отозван")
    return {"ok": True}
