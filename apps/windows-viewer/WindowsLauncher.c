#include <process.h>
#include <stdio.h>
#include <wchar.h>
#include <windows.h>

int wmain(void) {
    wchar_t directory[MAX_PATH];
    DWORD size = GetModuleFileNameW(NULL, directory, MAX_PATH);
    if (!size || size >= MAX_PATH) {
        MessageBoxW(NULL, L"Could not locate Remote Mac.", L"Remote Mac", MB_ICONERROR);
        return 1;
    }
    *wcsrchr(directory, L'\\') = L'\0';
    if (!SetCurrentDirectoryW(directory)) {
        MessageBoxW(NULL, L"Could not open the Remote Mac folder.", L"Remote Mac", MB_ICONERROR);
        return 1;
    }

    SetConsoleTitleW(L"Remote Mac");
    if (GetFileAttributesW(L"runtime\\python.exe") == INVALID_FILE_ATTRIBUTES) {
        MessageBoxW(NULL, L"Extract the entire ZIP before opening Remote Mac.exe.",
                    L"Remote Mac", MB_ICONERROR);
        return 1;
    }

    intptr_t status = _wspawnl(_P_WAIT, L"runtime\\python.exe", L"python.exe",
                              L"connect.py", NULL);
    if (status != 0) {
        fputws(L"\nPress Enter to close.\n", stderr);
        getwchar();
    }
    return status == 0 ? 0 : 1;
}
