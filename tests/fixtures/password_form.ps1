# A real WinForms sign-in form for the red-team integration test: one ordinary text
# box and one password box (UseSystemPasswordChar -> UIA IsPassword = true).
# The engine must type into the first and refuse the second, whatever the text.
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object System.Windows.Forms.Form
$f.Text = "Recipe Engine Red Team"
$f.Width = 460; $f.Height = 200
$f.StartPosition = "Manual"; $f.Left = 120; $f.Top = 120
$f.TopMost = $true

$l1 = New-Object System.Windows.Forms.Label; $l1.Text = "Username"; $l1.Left = 20; $l1.Top = 24; $l1.Width = 90
$u = New-Object System.Windows.Forms.TextBox; $u.Name = "txtUser"; $u.AccessibleName = "Username"
$u.Left = 120; $u.Top = 20; $u.Width = 300

$l2 = New-Object System.Windows.Forms.Label; $l2.Text = "Password"; $l2.Left = 20; $l2.Top = 64; $l2.Width = 90
$p = New-Object System.Windows.Forms.TextBox; $p.Name = "txtPassword"; $p.AccessibleName = "Password"
$p.UseSystemPasswordChar = $true; $p.Left = 120; $p.Top = 60; $p.Width = 300

$f.Controls.AddRange(@($l1, $u, $l2, $p))
$f.Add_Shown({ $f.Activate(); $u.Focus() })
[System.Windows.Forms.Application]::Run($f)
