"""Library routes: CRUD for library books."""
from fastapi import APIRouter, Request, Depends

from core.auth import verify_api_key
from services.shared_state import library_manager

router = APIRouter(prefix="/api/library", tags=["library"])


@router.get("")
async def get_library(_=Depends(verify_api_key)):
    return library_manager.get_all_books()


@router.post("/progress")
async def update_progress(request: Request, _=Depends(verify_api_key)):
    data = await request.json()
    book_id = data.get("book_id")
    progress = data.get("progress", 0)
    if book_id:
        library_manager.update_progress(book_id, float(progress))
    return {"status": "ok"}


@router.delete("/{book_id}")
async def delete_book(book_id: str, _=Depends(verify_api_key)):
    library_manager.delete_book(book_id)
    return {"status": "deleted"}
