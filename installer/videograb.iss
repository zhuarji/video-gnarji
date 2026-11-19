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
DefaultDirName={autopf}\VideoGrab
DefaultGroupName=VideoGrab
OutputDir=installer-output
OutputBaseFilename=Setup-VideoGrab
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
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
WelcomeLabel2=This will install VideoGrab on your computer.%n%nAll files (including the exe) go into ONE independent folder. Your downloads are kept in a "videoarji" subfolder next to the program.%n%nTip: keep the default install path, or choose a folder on D:\ etc. Avoid C:\Program Files (the program writes data next to itself).
