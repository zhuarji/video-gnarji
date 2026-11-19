; Inno Setup script for VideoGrab
; Built by GitHub Actions: ISCC installer/videograb.iss /DAppVersion=vX.Y.Z
; Produces a standard Windows installer: wizard, folder choice,
; shortcuts, uninstaller. All program files (including the exe)
; go into one independent folder.

#ifndef AppVersion
#define AppVersion "1.0.5"
#endif

[Setup]
AppName=VideoGrab
AppVersion={#AppVersion}
AppVerName=VideoGrab {#AppVersion}
DefaultDirName={sd}\VideoArji
DirExistsWarning=no
UsePreviousAppDir=yes
DefaultGroupName=VideoGrab
OutputDir=installer-output
OutputBaseFilename=Setup-VideoGrab
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayName=VideoGrab
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional:"

[Files]
Source: "..\dist\VideoGrab.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "README-install.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\VideoGrab"; Filename: "{app}\VideoGrab.exe"
Name: "{group}\Uninstall VideoGrab"; Filename: "{uninstallexe}"
Name: "{autodesktop}\VideoGrab"; Filename: "{app}\VideoGrab.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\VideoGrab.exe"; Description: "Launch VideoGrab now"; Flags: nowait postinstall skipifsilent

[Messages]
WelcomeLabel2=This will install VideoGrab on your computer.%n%nEverything goes into ONE independent folder (default: C:\VideoArji - you can pick any drive, e.g. D:\VideoArji). The exe, your downloads and ffmpeg all live inside that folder.
