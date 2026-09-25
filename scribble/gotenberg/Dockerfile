# lotek-gotenberg — Gotenberg with the report's fonts baked in.
#
# Why: Gotenberg's LibreOffice can only render fonts it has installed. Stock gotenberg/gotenberg:8 ships
# Liberation + DejaVu only, so a docx that asks for anything else (Inter, a code font, a brand face) is
# silently substituted — usually to a serif. Baking the fonts in makes the PDF deliverable's typography
# deterministic.
#
# Customizing is a drop-in: put any .ttf/.otf in fonts/ and rebuild — the WHOLE folder is installed and the
# fontconfig cache refreshed, so a new body/code face is available to the render with no other change. The
# report's default body (Inter) and code (JetBrains Mono) faces live here; add more to widen the operator's
# font menu.
#
# Build + tag (local dev):   docker build -t lotek-gotenberg:latest extensions/scribble/gotenberg
# Publish (prod, ops step):  docker tag lotek-gotenberg:latest <registry>/lotek-gotenberg:latest && push
# Point the managed container at it with SCRIBBLE_GOTENBERG_IMAGE, or ship it as the default once published.
FROM gotenberg/gotenberg:8

USER root
COPY fonts/ /usr/share/fonts/truetype/lotek/
RUN fc-cache -f && fc-list | sort | sed -n '1,40p'
USER gotenberg
