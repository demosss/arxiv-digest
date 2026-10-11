from typing import Annotated, Literal

from fastapi import APIRouter, File, Query, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import (
    CurrentUser,
    PaginationDep,
    ProfileDep,
    SessionDep,
    get_or_404,
    paginate,
)
from app.core.errors import AppError, ConflictError
from app.models import Topic
from app.schemas.common import Page
from app.schemas.topics import (
    ImportIssue,
    QueryValidateRequest,
    QueryValidateResponse,
    TopicCreate,
    TopicImportReport,
    TopicOut,
    TopicPreviewRequest,
    TopicPreviewResponse,
    TopicUpdate,
)
from app.services.arxiv_query import QueryCheck, check_query
from app.services.audit import apply_changes, audit
from app.services.topic_preview import TopicPreview, preview_topic
from app.services.topics_io import (
    ImportPlan,
    TopicData,
    apply_plan,
    build_report,
    export_topics_yaml,
    parse_topics_yaml,
    plan_import,
)

router = APIRouter(prefix="/topics", tags=["topics"])

_DUPLICATE = "Тема с таким названием уже существует"
MAX_IMPORT_BYTES = 1024 * 1024


@router.get("", response_model=Page[TopicOut])
async def list_topics(
    session: SessionDep,
    profile: ProfileDep,
    pagination: PaginationDep,
    is_active: bool | None = None,
    q: Annotated[str | None, Query(description="Поиск по названию")] = None,
) -> Page[TopicOut]:
    stmt = select(Topic).where(Topic.profile_id == profile.id).order_by(Topic.name)
    if is_active is not None:
        stmt = stmt.where(Topic.is_active == is_active)
    if q:
        stmt = stmt.where(Topic.name.icontains(q, autoescape=True))
    items, total = await paginate(session, stmt, pagination)
    return Page(
        items=[TopicOut.model_validate(t) for t in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post("", response_model=TopicOut, status_code=status.HTTP_201_CREATED)
async def create_topic(
    payload: TopicCreate, session: SessionDep, profile: ProfileDep, user: CurrentUser
) -> Topic:
    topic = Topic(profile_id=profile.id, **payload.model_dump())
    session.add(topic)
    try:
        await session.commit()
    except IntegrityError as exc:
        raise ConflictError(_DUPLICATE) from exc
    audit("create", "topic", topic.id, user.username, **payload.model_dump())
    return topic


@router.post("/validate-query", response_model=QueryValidateResponse)
async def validate_query(payload: QueryValidateRequest) -> QueryCheck:
    """Нормализует поисковый запрос и возвращает ошибки и предупреждения с позициями."""
    return check_query(payload.query)


@router.post("/preview", response_model=TopicPreviewResponse)
async def preview(payload: TopicPreviewRequest, user: CurrentUser) -> TopicPreview:
    """Что запросы находят на arXiv за последние `days` суток. В БД ничего не пишется.

    Одновременно выполняется один предпросмотр; второй получает 429 `preview_busy`.
    """
    result = await run_in_threadpool(
        preview_topic, payload.queries, payload.categories, payload.days
    )
    audit(
        "preview",
        "topic",
        None,
        user.username,
        queries=payload.queries,
        categories=payload.categories,
        days=payload.days,
        unique_total=result.unique_total,
    )
    return result


@router.get(
    "/export",
    response_class=Response,
    responses={200: {"content": {"application/x-yaml": {}}, "description": "Темы профиля в YAML"}},
)
async def export_topics(session: SessionDep, profile: ProfileDep, user: CurrentUser) -> Response:
    topics = (
        await session.scalars(
            select(Topic).where(Topic.profile_id == profile.id).order_by(Topic.name)
        )
    ).all()
    audit("export", "topic", None, user.username, topics=len(topics))
    return Response(
        export_topics_yaml(topics),
        media_type="application/x-yaml",
        headers={"Content-Disposition": 'attachment; filename="topics.yaml"'},
    )


@router.post("/import", response_model=TopicImportReport)
async def import_topics(
    session: SessionDep,
    profile: ProfileDep,
    user: CurrentUser,
    file: Annotated[UploadFile, File(description="YAML с ключом topics")],
    mode: Annotated[
        Literal["merge", "replace"],
        Query(description="merge — обновить по name и добавить новые; replace — ещё и удалить"),
    ] = "merge",
    dry_run: Annotated[bool, Query(description="Только показать, что изменится")] = False,
) -> TopicImportReport:
    """Импорт тем профиля из YAML. Запросы проверяются до записи: при ошибке не меняется ничего.

    С `dry_run=true` ошибки возвращаются в отчёте (200), без него — ответом 422 `import_invalid`.
    """
    raw = await file.read(MAX_IMPORT_BYTES + 1)
    items: list[TopicData]
    issues: list[ImportIssue]
    if len(raw) > MAX_IMPORT_BYTES:
        items, issues = [], [ImportIssue(loc=["file"], msg="Файл больше 1 МБ")]
    else:
        try:
            items, issues = parse_topics_yaml(raw.decode("utf-8"))
        except UnicodeDecodeError:
            items, issues = [], [ImportIssue(loc=["file"], msg="Файл должен быть в UTF-8")]

    plan = ImportPlan()
    if not issues:
        existing = (
            await session.scalars(select(Topic).where(Topic.profile_id == profile.id))
        ).all()
        plan = plan_import(existing, items, mode)
    report = build_report(plan, issues, mode=mode, dry_run=dry_run)

    if issues and not dry_run:
        raise AppError(
            422,
            "import_invalid",
            [issue.model_dump(exclude_none=True) for issue in issues],
        )
    if report.applied:
        session.add_all(apply_plan(plan, profile.id))
        for topic in plan.deleted:
            await session.delete(topic)
        await session.commit()
        audit(
            "import",
            "topic",
            None,
            user.username,
            mode=mode,
            created=report.created,
            updated=report.updated,
            deleted=report.deleted,
        )
    return report


@router.get("/{topic_id}", response_model=TopicOut)
async def get_topic(topic_id: int, session: SessionDep) -> Topic:
    return await get_or_404(session, Topic, topic_id)


@router.patch("/{topic_id}", response_model=TopicOut)
async def update_topic(
    topic_id: int, payload: TopicUpdate, session: SessionDep, user: CurrentUser
) -> Topic:
    topic = await get_or_404(session, Topic, topic_id)
    data = payload.model_dump(exclude_unset=True)
    # null допустим только для описания: так оно очищается
    changes = apply_changes(
        topic, {k: v for k, v in data.items() if v is not None or k == "description"}
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        raise ConflictError(_DUPLICATE) from exc
    audit("update", "topic", topic.id, user.username, changes=changes)
    return topic


@router.delete("/{topic_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topic(topic_id: int, session: SessionDep, user: CurrentUser) -> None:
    topic = await get_or_404(session, Topic, topic_id)
    name = topic.name
    await session.delete(topic)
    await session.commit()
    audit("delete", "topic", topic_id, user.username, name=name)
