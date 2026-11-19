VideoGrab - Local Video Downloader (Windows offline)
=====================================================

Built-in Python runtime. No installation needed.

FILES IN THIS FOLDER
--------------------
  Start-VideoGrab.bat      Run directly (use this first)
  Build-EXE.bat            Build a single-file VideoGrab.exe (PyInstaller)
  Make-EXE-Portable.bat    Build a portable exe with Windows built-in tool
                           (use this if Build-EXE fails or antivirus
                           quarantines the result)
  runtime\                 Python runtime - do not delete
  app.py / static\         Program
  downloads\               Videos are saved here
  buildtools\              Offline build wheels (can delete after build)
  build-log.txt            Build log (only after running Build-EXE)

HOW TO RUN (simplest)
---------------------
1. Extract the WHOLE folder (not a single file)
2. Double-click  Start-VideoGrab.bat
3. First run auto-downloads ffmpeg (~90MB, one time only)
4. Browser opens http://localhost:8080
5. Paste a video link, pick quality, download

Stop: close the black console window.

HOW TO GET A SINGLE-FILE EXE
----------------------------
Option A:  Double-click  Build-EXE.bat  (2-5 minutes)
           Result: dist\VideoGrab.exe  (25-40 MB)

If the exe comes out tiny (1 KB to a few MB) your ANTIVIRUS
quarantined it. Add this folder to the whitelist and retry,
or read build-log.txt.

Option B:  Double-click  Make-EXE-Portable.bat  (1-2 minutes)
           Uses Windows' built-in iexpress packer. Result:
           VideoGrab-Portable.exe in this folder.
           Double-clicking it extracts to %USERPROFILE%\VideoGrab
           and starts the service.

Both options produce an exe you can copy to any Windows PC
(no Python needed there either).

TROUBLESHOOTING
---------------
Q: Window flashes and closes instantly?
A: Antivirus blocked runtime\python.exe. Whitelist the folder.

Q: Port already in use?
A: Run:  runtime\python.exe app.py --port 3000
   Then open http://localhost:3000

Q: Phone access on same WiFi?
A: Run:  runtime\python.exe app.py --host 0.0.0.0 --port 8080
   Phone browser:  PC-IP:8080

Q: 1080p download fails / MP3 fails?
A: Check ffmpeg.exe in this folder. If missing, run
   Start-VideoGrab.bat again (auto-download) or place a
   manual ffmpeg.exe here (https://www.gyan.dev/ffmpeg/builds/)

Q: Parsing fails / site unsupported?
A: Update yt-dlp (needs internet):
     runtime\python.exe -m pip install -U yt-dlp --target runtime\Lib\site-packages

PRIVACY
-------
Runs fully local: links never touch any third-party server.
Paid / DRM-protected content cannot be downloaded - that is a
legal boundary the tool will not cross.
