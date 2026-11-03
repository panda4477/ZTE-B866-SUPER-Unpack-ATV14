# Extract Super — Linux / Ubuntu / WSL

เครื่องมือแตก Android `super.img` ออกเป็นไฟล์ logical partition `.img` โดยใช้ Python 3.8+ และอ่านไฟล์ต้นฉบับแบบ read-only

**ดัดแปลงจาก `extract_super.py` ที่ผู้ใช้อัปโหลด** ให้ใช้ใน Linux/WSL โดยตรง ไม่ต้องเรียก `wsl.exe` จาก Windows Python อีกต่อไป รองรับไฟล์ Android **raw** และ **sparse** (ผ่าน `simg2img`) และสร้างโฟลเดอร์ผลลัพธ์ตามวันและเวลาโดยอัตโนมัติ

## 1. เปิด WSL Ubuntu

หากยังไม่มี WSL ให้เปิด **Windows PowerShell แบบ Administrator** แล้วรันครั้งเดียว:

```powershell
wsl --install -d Ubuntu
```

ทำการรีสตาร์ต/ตั้งค่าบัญชี Ubuntu หากระบบร้องขอ จากนั้นเปิดแอป **Ubuntu** (หรือเรียก `wsl` ใน PowerShell) เพื่อรันคำสั่งต่อไปนี้ใน **Linux shell** ไม่ใช่ PowerShell

ตัวอย่าง ถ้าไฟล์อยู่ที่ `D:\ROM\super` ใน Windows ให้เข้าจาก WSL:

```bash
cd /mnt/d/ROM/super
```

หรือถ้าไฟล์อยู่ `C:\ROM\super` ให้ใช้ `/mnt/c/ROM/super`

## 2. ติดตั้งเครื่องมือ (ครั้งแรก)

วางไฟล์ `extract_super.py`, `install_tools.sh` ไว้โฟลเดอร์เดียวกัน แล้วรันใน Ubuntu:

```bash
bash ./install_tools.sh
```

สคริปต์ติดตั้งจาก APT ของ Ubuntu/Debian:

- `python3` — รันโปรแกรมหลัก
- `android-sdk-libsparse-utils` — คำสั่ง `simg2img` แปลง Android sparse เป็น raw
- `erofs-utils` — คำสั่ง `fsck.erofs` ตรวจสอบ/แตก EROFS
- `e2fsprogs` — คำสั่ง `debugfs` สำหรับตรวจสอบ ext4 ด้วยตัวเอง (โปรแกรมหลักไม่ mount ext4)

การติดตั้งใช้ `sudo` และอินเทอร์เน็ต แต่ **การแตกไฟล์ไม่จำเป็นต้องรัน sudo**

## 3. รันแตก `super.img`

ถ้า `super.img` อยู่ในโฟลเดอร์ที่เปิดอยู่:

```bash
python3 ./extract_super.py "./super.img"
```

โปรแกรมจะสร้างโฟลเดอร์ชื่อ เช่น:

```text
super/
├── extract_super.py
├── install_tools.sh
├── run_extract.sh
├── super.img
└── super_unpacked_2026-11-03_17-40-25/
    ├── partitions/
    │   ├── system_a.img
    │   ├── vendor_a.img
    │   └── product_a.img
    ├── system/                 # หาก system เป็น EROFS และแตกสำเร็จ
    ├── system_erofs.log        # log หากมีการแตก EROFS
    └── extraction_report.json
```

ไฟล์ .img ที่ปรากฏขึ้นจริงจะขึ้นอยู่กับพาร์ทิชันที่อยู่ใน `super.img` นั้น โฟลเดอร์ `system/` จะเกิดขึ้นเฉพาะเมื่อมีพาร์ทิชัน `system` ที่ตรวจพบว่าเป็น EROFS

**ใช้ตัวช่วย `.sh` ก็ได้:**

```bash
bash ./run_extract.sh "./super.img"
```

หากไม่ส่งพาธให้ `run_extract.sh` สคริปต์จะหา `super.img` ข้างไฟล์ `run_extract.sh` โดยอัตโนมัติ

## คำสั่งเพิ่มเติม

อ่านรายชื่อพาร์ทิชันโดยไม่แตกไฟล์:

```bash
python3 ./extract_super.py "./super.img" --list
```

แตกเฉพาะไฟล์ `.img` โดยไม่แตกไฟล์ภายใน EROFS:

```bash
python3 ./extract_super.py "./super.img" --images-only
```

เลือก metadata slot 1:

```bash
python3 ./extract_super.py "./super.img" --slot 1
```

แตก EROFS **ทุกพาร์ทิชัน** เข้าโฟลเดอร์ `files/` (ถ้า `fsck.erofs` รองรับการแตกลงไดเรกทอรี):

```bash
python3 ./extract_super.py "./super.img" --all-erofs
```

กำหนดโฟลเดอร์ผลลัพธ์เอง (ต้องเป็นชื่อโฟลเดอร์ที่ยังไม่มี):

```bash
python3 ./extract_super.py "./super.img" -o "./my_output"
```

อ้างอิงไฟล์บนไดรฟ์ Windows โดยตรง:

```bash
python3 ./extract_super.py "/mnt/d/ROM/super.img"
```

## หมายเหตุสำคัญ

1. **ไฟล์ต้นฉบับไม่ถูกแก้ไข** แต่การแตกไฟล์ต้องใช้พื้นที่ว่างเพิ่มเติมอย่างน้อยเท่ากับขนาดรวมของพาร์ทิชันที่แตก และอาจมากขึ้นสำหรับการแปลง sparse / แตกเนื้อหา EROFS
2. โปรแกรมรองรับ liblp super ที่มี **block device เดียว**; ไม่รองรับ super แบบ multi-device และไม่รวม/ประมวลผล snapshot `-cow` ที่ยังมี overlay
3. ถ้าเป็น `ext4` โปรแกรมจะส่งออก `.img` แต่ไม่ได้ mount หรือแตกไฟล์ ext4 เป็นโฟลเดอร์โดยอัตโนมัติ
4. **Ubuntu รุ่นเก่า** บางรุ่นมี `fsck.erofs` ที่รองรับเฉพาะ `--extract` (ตรวจสอบ filesystem) แต่ยังไม่รองรับ `--extract=DIR` (แตกไปเป็นโฟลเดอร์) ให้ใช้ `--images-only` หรืออัปเกรด `erofs-utils` เป็นรุ่นที่รองรับ โดยไฟล์พาร์ทิชัน `.img` ที่แตกแล้วจะยังคงอยู่
5. โปรแกรมตรวจสอบ SHA-256 ของผลลัพธ์ `.img` และ metadata checksum; ไม่ใช่เครื่องมือแฟลช ROM หรือปรับ AVB
6. เพื่อความเร็วและรองรับ symlink ของ EROFS ควรเก็บผลลัพธ์ภายใต้โฟลเดอร์ของ WSL เช่น `~/rom` หากเป็นไปได้ การเขียนลง `/mnt/c` หรือ `/mnt/d` อาจช้ากว่าและบางระบบไม่รองรับสิทธิ์/ลิงก์แบบ Linux ครบถ้วน
7. อุปกรณ์ใช้ virtual A/B อาจมี snapshot overlay ที่ไม่สามารถรวมโดยโปรแกรมนี้; ไม่ควรนำไฟล์ผลลัพธ์ไปแฟลชโดยไม่ตรวจสอบความถูกต้อง

## ทดสอบชุดจำลอง

```bash
python3 -m unittest discover -s tests -v
```

ชุดทดสอบใช้ข้อมูล liblp จำลอง ไม่ได้ทดสอบกับ `super.img` ของเครื่องจริง หรือการติดตั้ง APT/การทำงาน `fsck.erofs` จริงบน WSL

## แหล่งข้อมูลทางเทคนิค

- [Android liblp metadata format](https://android.googlesource.com/platform/system/core/+/refs/heads/main/fs_mgr/liblp/include/liblp/metadata_format.h)
- [EROFS tools installation](https://erofs.docs.kernel.org/en/latest/install.html)
- [fsck.erofs man page](https://manpages.debian.org/trixie/erofs-utils/fsck.erofs.1.en.html)
