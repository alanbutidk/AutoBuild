ifeq ($(OS),Windows_NT)
	PYNAME := python
else
	PYNAME := python3
endif
CC := -m nuitka
CFLAGS := --onefile \
		  --standalone \
		  --remove-output \
		  --no-pyi-file \
		  --assume-yes-for-downloads \
		  --nofollow-import-to=numpy \
		  --nofollow-import-to=pandas \
		  --nofollow-import-to=matplotlib \
		  --nofollow-import-to=scipy \
		  --nofollow-import-to=PIL \
		  --nofollow-import-to=tkinter \
		  --nofollow-import-to=PyQt5 \
		  --nofollow-import-to=PyQt6 \
		  --nofollow-import-to=wx \
		  --nofollow-import-to=django \
		  --nofollow-import-to=flask \
		  --nofollow-import-to=fastapi \
		  --nofollow-import-to=sqlalchemy \
		  --nofollow-import-to=requests \
		  --nofollow-import-to=urllib3 \
		  --nofollow-import-to=cryptography \
		  --nofollow-import-to=ssl \
		  --nofollow-import-to=unittest \
		  --nofollow-import-to=pydoc \
		  --nofollow-import-to=doctest \
		  --nofollow-import-to=turtle \
		  --nofollow-import-to=curses \
		  --nofollow-import-to=idlelib \
		  --nofollow-import-to=lib2to3 \
		  --nofollow-import-to=distutils \
		  --nofollow-import-to=ensurepip \
		  --nofollow-import-to=venv \
		  --nofollow-import-to=setuptools \
		  --nofollow-import-to=pkg_resources \
		  --nofollow-import-to=pip \
		  --nofollow-import-to=email \
		  --nofollow-import-to=html \
		  --nofollow-import-to=http \
		  --nofollow-import-to=xml \
		  --nofollow-import-to=xmlrpc \
		  --nofollow-import-to=multiprocessing \
		  --nofollow-import-to=concurrent \
		  --nofollow-import-to=asyncio \
		  --nofollow-import-to=ctypes \
		  --nofollow-import-to=logging \
		  --nofollow-import-to=unittest \
		  --nofollow-import-to=test
TARGET := autobuild

ifeq ($(OS),Windows_NT)
	executable := .exe
	RM := del /f /q
	RMDIR := rmdir /s /q
else
	executable :=
	RM := rm -f
	RMDIR := rm -rf
endif

all: build

build:
	$(PYNAME) $(CC) $(CFLAGS) --output-filename=$(TARGET)$(executable) autobuild.py

clean:
	$(RM) $(TARGET)$(executable) *.o *.pyi
	-$(RMDIR) autobuild.build 2>nul
	-$(RMDIR) autobuild.dist 2>nul
	-$(RMDIR) autobuild.onefile-build 2>nul

.PHONY: build clean
