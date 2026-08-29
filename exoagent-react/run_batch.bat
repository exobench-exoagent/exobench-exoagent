@echo off
setlocal EnableExtensions

rem Edit these calls so each directory is paired with the planet on the same line.
call :run_one "/absolute/path/to/fits-directory-1" "Planet name 1"
call :run_one "/absolute/path/to/fits-directory-2" "Planet name 2"

if "%FAILURES%"=="" set "FAILURES=0"
if not "%FAILURES%"=="0" (
  echo %FAILURES% run^(s^) failed. 1>&2
  exit /b 1
)

echo All runs finished successfully.
exit /b 0

:run_one
set "INPUT_DIR=%~1"
set "PLANET=%~2"

echo Running main.py
echo Input directory: %INPUT_DIR%
echo Planet: %PLANET%

(
  echo(%INPUT_DIR%
  echo(%PLANET%
) | python main.py

if errorlevel 1 (
  set /a FAILURES+=1
  echo Run failed. 1>&2
) else (
  echo Finished successfully.
)

echo.
exit /b 0
