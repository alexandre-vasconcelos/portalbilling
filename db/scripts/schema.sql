CREATE TABLE dbo.BillingRecords (
  Id INT IDENTITY(1,1) PRIMARY KEY,
  TenancyOcid VARCHAR(255) NULL,
  Region VARCHAR(64) NULL,
  AvailabilityDomain VARCHAR(64) NULL,
  SkuPartNumber VARCHAR(64) NULL,
  TagsJson NVARCHAR(MAX) NULL,
  UsageDate DATETIME2 NOT NULL,
  ServiceName VARCHAR(255) NULL,
  ResourceName VARCHAR(255) NULL,
  Cost DECIMAL(18,4) NOT NULL,
  Currency VARCHAR(10) NULL,
  UsageQuantity DECIMAL(18,4) NULL,
  UsageUnit VARCHAR(32) NULL,
  CreatedAt DATETIME2 NOT NULL DEFAULT (SYSUTCDATETIME())
);

CREATE TABLE dbo.Users (
  Id INT IDENTITY(1,1) PRIMARY KEY,
  Username VARCHAR(100) NOT NULL UNIQUE,
  PasswordHash VARCHAR(255) NOT NULL,
  Role VARCHAR(20) NOT NULL, -- 'admin' ou 'user'
  IsActive BIT NOT NULL DEFAULT (1),
  CreatedAt DATETIME2 NOT NULL DEFAULT (SYSUTCDATETIME())
);

CREATE TABLE dbo.SyncSettings (
  Id INT IDENTITY(1,1) PRIMARY KEY,
  AutoEnabled BIT NOT NULL DEFAULT (1),
  CronExpression VARCHAR(64) NOT NULL DEFAULT ('0 3 * * *'),
  TimeZone VARCHAR(64) NOT NULL DEFAULT ('UTC'),
  IsSyncRunning BIT NOT NULL DEFAULT (0),
  LastManualStart DATETIME2 NULL,
  LastManualEnd DATETIME2 NULL,
  DailyLimit DECIMAL(18,2) NULL,
  DailyLimitCurrency VARCHAR(10) NULL,
  MonthlyBudget DECIMAL(18,2) NULL,
  MonthlyBudgetCurrency VARCHAR(10) NULL,
  LastUsageDateUtc DATETIME2 NULL,
  LastRateLimitAtUtc DATETIME2 NULL,
  ForecastUntilDate DATE NULL
);

CREATE TABLE dbo.SyncRuns (
  Id INT IDENTITY(1,1) PRIMARY KEY,
  IsManual BIT NOT NULL,
  StartedAt DATETIME2 NOT NULL,
  CompletedAt DATETIME2 NULL,
  Status VARCHAR(20) NOT NULL,
  ErrorMessage VARCHAR(4000) NULL,
  RecordsReturned INT NULL,
  RecordsInserted INT NULL,
  WindowStart DATETIME2 NULL,
  WindowEnd DATETIME2 NULL
);
