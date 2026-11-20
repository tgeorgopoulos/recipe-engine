# "The user's window": a plain WinForms form the integration tests bring to the
# foreground before a BACKGROUND replay, to prove the replay never takes it away.
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object System.Windows.Forms.Form
$f.Text = "User Window (Recipe Engine test)"
$f.Width = 420; $f.Height = 160
$f.StartPosition = "Manual"; $f.Left = 700; $f.Top = 500
$l = New-Object System.Windows.Forms.Label; $l.Text = "I am what the user is looking at."; $l.Left = 20; $l.Top = 40; $l.Width = 360
$f.Controls.Add($l)
$f.Add_Shown({ $f.Activate() })
[System.Windows.Forms.Application]::Run($f)
