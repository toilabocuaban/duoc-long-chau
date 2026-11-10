$ErrorActionPreference='Stop'
$cad=[Runtime.InteropServices.Marshal]::GetActiveObject('AutoCAD.Application')
$doc=$cad.ActiveDocument
$origin=@(351421.53136008,239481.1168709311)
$rows=@()
$architecture=$doc.ModelSpace.Item(0)
$offset=$architecture.InsertionPoint
foreach($e in $doc.Blocks.Item('asnsalda')){
    if($e.ObjectName -eq 'AcDbBlockReference'){
        $p=$e.InsertionPoint
        $x=($p[0]+$offset[0]-$origin[0])/1000; $y=($p[1]+$offset[1]-$origin[1])/1000
        if($x -gt -60 -and $x -lt 5 -and $y -gt -30 -and $y -lt -8){
            $lo=$null;$hi=$null; $e.GetBoundingBox([ref]$lo,[ref]$hi)
            $rows += [pscustomobject]@{handle=$e.Handle;name=$e.Name;effective=$e.EffectiveName;layer=$e.Layer;x=$x;y=$y;rotation=$e.Rotation;bounds=@((($lo[0]+$offset[0]-$origin[0])/1000),(($lo[1]+$offset[1]-$origin[1])/1000),(($hi[0]+$offset[0]-$origin[0])/1000),(($hi[1]+$offset[1]-$origin[1])/1000))}
        }
    }
}
$rows|ConvertTo-Json -Depth 5
