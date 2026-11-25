from dataclasses import dataclass
from datetime import timedelta
import hashlib
import secrets
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import get_db
from .config import get_settings
from .models import AuthSession, User, Membership, Organization, Campaign, now

hasher = PasswordHasher()
COOKIE_NAME = "nira_session"
DUMMY_HASH = hasher.hash("unusable-password-for-timing-only")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def verify_password(password, encoded):
    try:
        return hasher.verify(encoded or DUMMY_HASH, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def issue_session(db, user_id, organization_id, response: Response):
    token = secrets.token_urlsafe(48)
    csrf = digest(token + ":csrf")
    settings = get_settings()
    db.add(AuthSession(user_id=user_id, organization_id=organization_id, token_hash=digest(token), csrf_hash=digest(csrf), expires_at=now() + timedelta(hours=settings.session_hours)))
    response.set_cookie(COOKIE_NAME, token, max_age=settings.session_hours * 3600, httponly=True, secure=settings.cookie_secure, samesite="strict", path="/")
    return csrf


@dataclass
class Actor:
    user: User
    membership: Membership
    organization: Organization
    session: AuthSession
    csrf: str

    def can(self, permission):
        if self.membership.role == "owner":
            return True
        defaults = {"view_team", "view_counterparties", "approve", "send", "export"} if self.membership.role == "manager" else {"view_counterparties", "export"}
        if self.membership.role == "viewer":
            defaults = {"view_counterparties", "export"}
        return self.membership.permissions.get(permission, permission in defaults)

    def require(self, permission):
        if not self.can(permission):
            raise HTTPException(403, "Недостаточно прав для этого действия")

    def write(self):
        if self.membership.role == "viewer":
            raise HTTPException(403, "Наблюдатель может только просматривать данные")

    def owner(self):
        if self.membership.role != "owner":
            raise HTTPException(403, "Действие доступно владельцу организации")


def get_actor(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "Войдите в аккаунт")
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest(token), AuthSession.expires_at > now()))
    if not session:
        raise HTTPException(401, "Сессия истекла")
    user = db.get(User, session.user_id)
    membership = db.scalar(select(Membership).where(Membership.user_id == session.user_id, Membership.organization_id == session.organization_id, Membership.status == "active"))
    if not user or not user.active or not membership:
        raise HTTPException(401, "Доступ отозван")
    csrf = digest(token + ":csrf")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        provided = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(digest(provided), session.csrf_hash):
            raise HTTPException(403, "Некорректный CSRF-токен")
    return Actor(user, membership, db.get(Organization, session.organization_id), session, csrf)


def tenant_object(db, model, identifier, actor):
    obj = db.scalar(select(model).where(model.id == identifier, model.organization_id == actor.organization.id))
    if obj is None:
        raise HTTPException(404, "Объект не найден")
    return obj


def campaign_object(db, identifier, actor):
    campaign = tenant_object(db, Campaign, identifier, actor)
    if not actor.can("view_team") and campaign.owner_id != actor.user.id and campaign.created_by != actor.user.id:
        raise HTTPException(404, "Задача не найдена")
    return campaign


def visible_campaigns(actor):
    query = select(Campaign).where(Campaign.organization_id == actor.organization.id)
    if not actor.can("view_team"):
        query = query.where((Campaign.owner_id == actor.user.id) | (Campaign.created_by == actor.user.id))
    return query
