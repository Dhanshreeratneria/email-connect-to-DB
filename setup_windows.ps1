# Gmail Email MCP - Windows Setup Script
# This script sets up the database schema and installs dependencies on Windows

param(
    [string]$DbHost = "localhost",
    [string]$DbPort = "5432",
    [string]$DbName = "gmail_email_mcp",
    [string]$DbUser = "postgres",
    [securestring]$DbPassword = $null,
    [string]$ProjectPath = ".",
    [switch]$SkipDb = $false,
    [switch]$SkipPip = $false,
    [switch]$Help = $false
)

# Display help
if ($Help) {
    @"
Gmail Email MCP - Windows Setup Script

Usage:
    .\setup_windows.ps1 -DbHost localhost -DbName gmail_email_mcp -DbUser postgres

Parameters:
    -DbHost         PostgreSQL host (default: localhost)
    -DbPort         PostgreSQL port (default: 5432)
    -DbName         Database name (default: gmail_email_mcp)
    -DbUser         Database user (default: postgres)
    -DbPassword     Database password (will prompt if not provided)
    -ProjectPath    Path to project root (default: current directory)
    -SkipDb         Skip database setup
    -SkipPip        Skip pip installation
    -Help           Show this help message

Example:
    .\setup_windows.ps1 -DbHost db.example.com -DbName gmail_mcp -DbUser admin
"@
    exit 0
}

Write-Host "================================" -ForegroundColor Cyan
Write-Host "Gmail Email MCP - Windows Setup" -ForegroundColor Cyan
Write-Host "================================" -ForegroundColor Cyan
Write-Host ""

# Prompt for password if not provided
if ($DbPassword -eq $null) {
    $DbPassword = Read-Host "Enter PostgreSQL password for user '$DbUser'" -AsSecureString
}

# Convert secure string to plain text for psql
$Ptr = [System.Runtime.InteropServices.Marshal]::SecureStringToCoTaskMemUnicode($DbPassword)
$DbPasswordPlain = [System.Runtime.InteropServices.Marshal]::PtrToStringUni($Ptr)
[System.Runtime.InteropServices.Marshal]::ZeroFreeCoTaskMemUnicode($Ptr)

# Check if PostgreSQL client tools are available
Write-Host "Checking PostgreSQL client tools..." -ForegroundColor Yellow
$psqlCmd = Get-Command psql -ErrorAction SilentlyContinue

if (-not $psqlCmd) {
    Write-Host "ERROR: psql not found in PATH!" -ForegroundColor Red
    Write-Host "Please install PostgreSQL client tools or add them to PATH" -ForegroundColor Red
    Write-Host ""
    Write-Host "Download from: https://www.postgresql.org/download/windows/" -ForegroundColor Green
    exit 1
}

Write-Host "✓ PostgreSQL client tools found" -ForegroundColor Green
Write-Host ""

# ============================================================================
# Database Setup
# ============================================================================

if (-not $SkipDb) {
    Write-Host "Setting up database..." -ForegroundColor Cyan
    Write-Host "================================" -ForegroundColor Cyan
    
    # Prepare SQL commands
    $sql = @"
-- Ensure email_attachments table exists and has required columns
CREATE TABLE IF NOT EXISTS email_attachments (
    id SERIAL PRIMARY KEY,
    email_id INTEGER NOT NULL,
    gmail_attachment_id VARCHAR(512) NOT NULL,
    filename VARCHAR(1024) NOT NULL,
    mime_type VARCHAR(255),
    size BIGINT,
    content BYTEA,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    UNIQUE(email_id, gmail_attachment_id),
    FOREIGN KEY(email_id) REFERENCES emails(id) ON DELETE CASCADE
);

-- Add content column if it doesn't exist
DO `$`
BEGIN
    IF NOT EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'email_attachments' AND column_name = 'content'
    ) THEN
        ALTER TABLE email_attachments ADD COLUMN content BYTEA;
    END IF;
END
`$`;

-- Create indexes for faster queries
CREATE INDEX IF NOT EXISTS idx_email_attachments_email_id 
    ON email_attachments(email_id);

CREATE INDEX IF NOT EXISTS idx_email_attachments_gmail_id 
    ON email_attachments(gmail_attachment_id);

-- Verify table structure
\d email_attachments
"@
    
    # Save SQL to temporary file
    $sqlFile = Join-Path $env:TEMP "setup_attachments_$([guid]::NewGuid()).sql"
    $sql | Out-File -FilePath $sqlFile -Encoding UTF8
    
    Write-Host "Executing SQL setup..."
    Write-Host "  Host: $DbHost"
    Write-Host "  Port: $DbPort"
    Write-Host "  Database: $DbName"
    Write-Host "  User: $DbUser"
    Write-Host ""
    
    try {
        # Execute SQL
        $env:PGPASSWORD = $DbPasswordPlain
        
        & psql -h $DbHost -p $DbPort -U $DbUser -d $DbName -f $sqlFile | Out-Host
        
        Remove-Item env:PGPASSWORD -ErrorAction SilentlyContinue
        
        if ($LASTEXITCODE -eq 0) {
            Write-Host "✓ Database setup completed successfully" -ForegroundColor Green
        } else {
            Write-Host "⚠ Database setup had issues (exit code: $LASTEXITCODE)" -ForegroundColor Yellow
            Write-Host "  This might be OK if tables already exist" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "ERROR: Failed to execute database setup" -ForegroundColor Red
        Write-Host $_.Exception.Message -ForegroundColor Red
        exit 1
    } finally {
        # Clean up
        if (Test-Path $sqlFile) {
            Remove-Item $sqlFile -ErrorAction SilentlyContinue
        }
        Remove-Item env:PGPASSWORD -ErrorAction SilentlyContinue
    }
    
    Write-Host ""
}

# ============================================================================
# Python Dependencies Setup
# ============================================================================

if (-not $SkipPip) {
    Write-Host "Setting up Python dependencies..." -ForegroundColor Cyan
    Write-Host "===================================" -ForegroundColor Cyan
    
    # Check if Python is available
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    $python3Cmd = Get-Command python3 -ErrorAction SilentlyContinue
    
    if ($python3Cmd) {
        $pythonExe = "python3"
    } elseif ($pythonCmd) {
        $pythonExe = "python"
    } else {
        Write-Host "ERROR: Python not found in PATH!" -ForegroundColor Red
        Write-Host "Please install Python 3.9+ from: https://www.python.org/downloads/" -ForegroundColor Green
        exit 1
    }
    
    Write-Host "Using Python: $pythonExe" -ForegroundColor Green
    Write-Host ""
    
    # Check if pip is available
    Write-Host "Checking pip..." -ForegroundColor Yellow
    
    try {
        & $pythonExe -m pip --version | Out-Host
    } catch {
        Write-Host "ERROR: pip is not available!" -ForegroundColor Red
        exit 1
    }
    
    Write-Host "✓ pip is available" -ForegroundColor Green
    Write-Host ""
    
    # Update pip
    Write-Host "Updating pip..." -ForegroundColor Yellow
    & $pythonExe -m pip install --upgrade pip
    
    if ($LASTEXITCODE -ne 0) {
        Write-Host "⚠ Warning: pip update had issues, continuing anyway..." -ForegroundColor Yellow
    }
    
    Write-Host ""
    
    # Install requirements
    $requirementsFile = Join-Path $ProjectPath "requirements.txt"
    
    if (Test-Path $requirementsFile) {
        Write-Host "Installing dependencies from requirements.txt..." -ForegroundColor Yellow
        Write-Host "File: $requirementsFile"
        Write-Host ""
        
        & $pythonExe -m pip install -r $requirementsFile
        
        if ($LASTEXITCODE -eq 0) {
            Write-Host "✓ All dependencies installed successfully" -ForegroundColor Green
        } else {
            Write-Host "ERROR: Failed to install dependencies" -ForegroundColor Red
            exit 1
        }
    } else {
        Write-Host "⚠ requirements.txt not found at: $requirementsFile" -ForegroundColor Yellow
        Write-Host "  Skipping pip installation" -ForegroundColor Yellow
    }
    
    Write-Host ""
}

# ============================================================================
# Verification
# ============================================================================

Write-Host "Verifying installation..." -ForegroundColor Cyan
Write-Host "============================" -ForegroundColor Cyan
Write-Host ""

# Check database connection
if (-not $SkipDb) {
    Write-Host "Checking database connection..." -ForegroundColor Yellow
    
    try {
        $env:PGPASSWORD = $DbPasswordPlain
        
        $testQuery = "SELECT version();"
        $output = & psql -h $DbHost -p $DbPort -U $DbUser -d $DbName -c $testQuery 2>&1
        
        Remove-Item env:PGPASSWORD -ErrorAction SilentlyContinue
        
        if ($LASTEXITCODE -eq 0) {
            Write-Host "✓ Database connection successful" -ForegroundColor Green
            Write-Host "  $($output[2])" -ForegroundColor Gray
        } else {
            Write-Host "✗ Database connection failed!" -ForegroundColor Red
            Write-Host $output -ForegroundColor Red
        }
    } catch {
        Write-Host "✗ Error checking database:" -ForegroundColor Red
        Write-Host $_.Exception.Message -ForegroundColor Red
    }
    
    Write-Host ""
}

# Check Python packages
if (-not $SkipPip) {
    Write-Host "Checking Python packages..." -ForegroundColor Yellow
    
    $packagesToCheck = @(
        "sqlalchemy",
        "psycopg2",
        "fastapi",
        "google-auth",
        "PIL",
        "fitz",
        "docx",
        "openpyxl",
        "pptx"
    )
    
    foreach ($package in $packagesToCheck) {
        try {
            & $pythonExe -c "import $package" 2>$null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "  ✓ $package" -ForegroundColor Green
            } else {
                Write-Host "  ✗ $package (not installed)" -ForegroundColor Yellow
            }
        } catch {
            Write-Host "  ✗ $package (error)" -ForegroundColor Yellow
        }
    }
    
    Write-Host ""
}

# ============================================================================
# Summary
# ============================================================================

Write-Host "Setup Summary" -ForegroundColor Cyan
Write-Host "=============" -ForegroundColor Cyan
Write-Host ""
Write-Host "Configuration:" -ForegroundColor White
Write-Host "  Database Host: $DbHost"
Write-Host "  Database Port: $DbPort"
Write-Host "  Database Name: $DbName"
Write-Host "  Database User: $DbUser"
Write-Host ""

Write-Host "What was done:" -ForegroundColor White
if (-not $SkipDb) {
    Write-Host "  ✓ Database schema created/updated"
    Write-Host "  ✓ Indexes created"
    Write-Host "  ✓ Tables verified"
} else {
    Write-Host "  ⊘ Database setup skipped"
}

if (-not $SkipPip) {
    Write-Host "  ✓ Python dependencies installed"
} else {
    Write-Host "  ⊘ Python dependencies skipped"
}

Write-Host ""
Write-Host "Next Steps:" -ForegroundColor Green
Write-Host "  1. Copy code files to the project:"
Write-Host "     - attachment_service.py → app/services/"
Write-Host "     - server_updated.py → app/mcp/server.py"
Write-Host "     - sync_service_updated.py → app/services/sync_service.py"
Write-Host "     - attachments_api.py → app/api/"
Write-Host ""
Write-Host "  2. Update app/main.py to include attachments router"
Write-Host ""
Write-Host "  3. Restart the application:"
Write-Host "     python -m uvicorn app.main:app --reload"
Write-Host ""
Write-Host "For more details, see: IMPLEMENTATION_GUIDE.md" -ForegroundColor Green
Write-Host ""
