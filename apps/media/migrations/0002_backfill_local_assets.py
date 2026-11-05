"""Spec 15 §15.1: a local MediaAsset for every image already on disk, linked from its record. File names stay as they are."""
from django.db import migrations


def _stem(name):
    return name.rsplit("/", 1)[-1].split("_", 1)[0]


def forwards(apps, schema_editor):
    MediaAsset = apps.get_model("media", "MediaAsset")
    targets = [
        ("candidates", "Candidate", "photo_800", "photo_200", "photo_asset", "800", "200", lambda o: "candidate_photo"),
        ("news", "Article", "image", "image_thumb", "image_asset", "image", "thumb",
         lambda o: "announcement_image" if o.source == "official" else "news_image"),
        ("posts", "CandidatePost", "image", "image_thumb", "image_asset", "image", "thumb", lambda o: "post_image"),
    ]
    for app, model, main, small, fk, k1, k2, purpose in targets:
        M = apps.get_model(app, model)
        for o in M.objects.exclude(**{main: ""}).filter(**{f"{fk}__isnull": True}).iterator():
            name = getattr(o, main)
            if name.startswith("cld:"):
                continue
            files = {k1: name}
            if getattr(o, small):
                files[k2] = getattr(o, small)
            asset, _ = MediaAsset.objects.get_or_create(
                provider="local", public_id=_stem(name),
                defaults={"purpose": purpose(o), "local_files": files, "secure_url": name, "format": "webp",
                          "width": getattr(o, "image_width", None), "height": getattr(o, "image_height", None)})
            M.objects.filter(pk=o.pk).update(**{fk: asset})


class Migration(migrations.Migration):
    dependencies = [("media", "0001_initial"), ("candidates", "0002_candidate_photo_asset"), ("news", "0004_article_image_asset"),
                    ("posts", "0004_candidatepost_image_asset")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
