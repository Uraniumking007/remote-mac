Unicode true
RequestExecutionLevel user
SetCompressor /SOLID lzma

!ifndef APP_VERSION
  !error "Pass -DAPP_VERSION=x.y.z"
!endif
!ifndef STAGE
  !error "Pass -DSTAGE=path to the viewer bundle"
!endif
!ifndef OUTFILE
  !error "Pass -DOUTFILE=path to the setup executable"
!endif

!define APP_NAME "Remote Mac"
!define INSTALL_LEAF "\Programs\Remote Mac"
!define INSTALL_KEY "Software\Remote Mac"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Remote Mac"

Name "${APP_NAME}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\Remote Mac"
Caption "${APP_NAME} ${APP_VERSION} Setup"
BrandingText "${APP_NAME} ${APP_VERSION}"
ShowInstDetails show
ShowUninstDetails show

VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey /LANG=1033 "ProductName" "${APP_NAME}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "FileDescription" "${APP_NAME} Setup"
VIAddVersionKey /LANG=1033 "LegalCopyright" "${APP_NAME}"
VIAddVersionKey /LANG=1033 "OriginalFilename" "Remote-Mac-Windows-x64-${APP_VERSION}-Setup.exe"

!ifdef APP_ICON
  Icon "${APP_ICON}"
  UninstallIcon "${APP_ICON}"
!endif

!include LogicLib.nsh
!include x64.nsh
!include FileFunc.nsh

; Key mapping is %LOCALAPPDATA%\Remote Mac\key-mapping.json. This install
; directory stays under Programs so uninstall cannot remove that file.
!macro EnsureOwnDirectory
  StrCpy $1 "${INSTALL_LEAF}"
  StrLen $2 $1
  StrCpy $3 $INSTDIR "" -$2
  ${If} $3 != $1
    MessageBox MB_ICONSTOP "Remote Mac only installs to its folder under LocalAppData\Programs."
    Abort
  ${EndIf}
!macroend

Function .onInit
  SetShellVarContext current
  ${If} ${RunningX64}
    SetRegView 64
  ${EndIf}
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\Remote Mac"
  !insertmacro EnsureOwnDirectory
  ${If} ${FileExists} "$INSTDIR\Uninstall.exe"
    ExecWait '"$INSTDIR\Uninstall.exe" /S _?=$INSTDIR'
    Delete "$INSTDIR\Uninstall.exe"
    RMDir "$INSTDIR"
  ${EndIf}
FunctionEnd

Page instfiles

Section
  SetOutPath "$INSTDIR"
  File /r "${STAGE}/*.*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  WriteRegStr HKCU "${INSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "${APP_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\Remote Mac.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "EstimatedSize" $0

  SetShellVarContext current
  CreateDirectory "$SMPROGRAMS\Remote Mac"
  CreateShortCut "$SMPROGRAMS\Remote Mac\Remote Mac.lnk" "$INSTDIR\Remote Mac.exe"
  CreateShortCut "$SMPROGRAMS\Remote Mac\Uninstall Remote Mac.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Function un.onInit
  SetShellVarContext current
  ${If} ${RunningX64}
    SetRegView 64
  ${EndIf}
  !insertmacro EnsureOwnDirectory
FunctionEnd

UninstPage uninstConfirm
UninstPage instfiles

Section "Uninstall"
  RMDir /r "$INSTDIR"
  Delete "$SMPROGRAMS\Remote Mac\Remote Mac.lnk"
  Delete "$SMPROGRAMS\Remote Mac\Uninstall Remote Mac.lnk"
  RMDir "$SMPROGRAMS\Remote Mac"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegKey HKCU "${INSTALL_KEY}"
SectionEnd
