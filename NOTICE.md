# Licensing scope / third-party material

Our own work in this repository — the documentation (`README-RU.md`, `REPORT-EN.md`,
`README.md`), the flasher (`ace_flash.py`), the stub sources (`artefacts_stub4.s`,
`artefacts_stub_tunnel5.s`), the builder (`artefacts_build_tunnel5.py`) and the
patch recipe — is licensed under **GPL-3.0** (see `LICENSE`).

The **firmware images are third-party material and are NOT covered by that grant**:

* `ACE_V1.3.863_20260716.bin` — the unmodified community firmware (CFW),
  byte-identical to the asset `ACE_V1.3.863_20260716.bin` of the **OpenCubic
  v1.0.2 release** (md5 `9f7b9a678a96caf98d6a08842d3ff971`, 113720 B).
  The OpenCubic project publishes **no licence file**; the image remains © its authors.
* `ACE_V1.3.863_cfw_uid.bin` — the same CFW with our 4-byte hook and 108-byte
  appended stub. Same provenance and same caveat as the base image.
* `ACE_V1.3.863_tunnel5.bin` — derived the same way from the vendor image.
* `ACE_V1.3.863_stock.bin` — the **vendor (Anycubic)** firmware image, included
  solely as the rollback reference. © Anycubic; no licence is granted here.

Nothing in this repository transfers or grants rights to those images. They are
provided as-is for interoperability research and for returning a device to stock.
Flashing them is at your own risk.
