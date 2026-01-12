-- Cria um usuário admin de teste para acesso ao portal
-- Login: admin / Admin@123

IF NOT EXISTS (SELECT 1 FROM dbo.Users WHERE Username = 'admin')
BEGIN
    INSERT INTO dbo.Users (Username, PasswordHash, Role, IsActive)
    VALUES (
        'admin',
        'pbkdf2:sha256:260000$jFrCHU8oKHeZx0W3yYAmRA==$IwpOLcH6QPOmQoW0zZJv4ZJROHyopo5PH3+Ze9V+gWU=',
        'admin',
        1
    );
END;

