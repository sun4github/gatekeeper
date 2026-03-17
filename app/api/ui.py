from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def read_root():
    with open("index.html") as f:
        return f.read()


@router.get("/api/v1/ui/manifest.webmanifest")
async def ui_manifest():
    """Web app manifest served via API path for reverse-proxy compatibility."""
    return JSONResponse(
        content={
            "name": "Gatekeeper",
            "short_name": "Gatekeeper",
            "description": "Parental control console for internet and service blocking.",
            "id": "/gatekeeper/",
            "start_url": "/gatekeeper/",
            "scope": "/gatekeeper/",
            "display": "standalone",
            "background_color": "#131926",
            "theme_color": "#224f6e",
            "icons": [
                {
                    "src": "/gatekeeper/api/v1/ui/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/gatekeeper/api/v1/ui/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/gatekeeper/api/v1/ui/icon-maskable-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
        },
        media_type="application/manifest+json",
    )


@router.get("/api/v1/ui/manifest")
async def ui_manifest_alias():
    """Alias path for proxies that do not forward dotted filenames reliably."""
    return await ui_manifest()


@router.get("/api/v1/ui/icon-192.png")
async def ui_icon_192_png():
    return FileResponse("assets/icon-192.png", media_type="image/png")


@router.get("/api/v1/ui/icon-192")
async def ui_icon_192_png_alias():
    return await ui_icon_192_png()


@router.get("/api/v1/ui/icon-512.png")
async def ui_icon_512_png():
    return FileResponse("assets/icon-512.png", media_type="image/png")


@router.get("/api/v1/ui/icon-512")
async def ui_icon_512_png_alias():
    return await ui_icon_512_png()


@router.get("/api/v1/ui/icon-maskable-512.png")
async def ui_icon_maskable_512_png():
    return FileResponse("assets/icon-maskable-512.png", media_type="image/png")


@router.get("/api/v1/ui/icon-maskable-512")
async def ui_icon_maskable_512_png_alias():
    return await ui_icon_maskable_512_png()


@router.get("/api/v1/ui/icon.svg")
async def ui_icon_svg():
    return FileResponse("assets/icon.svg", media_type="image/svg+xml")


@router.get("/api/v1/ui/icon")
async def ui_icon_svg_alias():
    return await ui_icon_svg()
