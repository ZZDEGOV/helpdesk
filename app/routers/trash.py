"""Records queued for deletion, restorable until the retention window passes.
Expired records are purged by the background sweep in main.py."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import repo
from ..deps import DB, TrashViewer
from ..web import render

router = APIRouter(tags=["trash"])


@router.get("/trash", response_class=HTMLResponse, summary="Deletion queue")
def trash(request: Request, conn: DB, user: TrashViewer):
    return render(request, "trash.html", {"d": repo.list_deleted(conn)})
