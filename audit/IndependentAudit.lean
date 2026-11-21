import Lean

/-!
# Audit imported declarations without project frontend extensions

Main is loaded as data at runtime, never as a frontend import. The expected
propositions must have been compiled with Mathlib alone. `withImportModules`
disables imported environment extensions; the axiom walk reads private kernel
declarations rather than exported axiom summaries. This uses the same Lean
kernel implementation as the replay and does not replace fresh kernel replay.
-/
open Lean

/-- Walk types, proof bodies, opaque bodies, and inductive constructors. Fail on
unknown constants; a missing proof dependency must never count as axiom-free. -/
def rawAxioms (env : Environment) (roots : Array Name) : Except String (Array Name × Nat) := do
  let mut seen : Std.HashSet Name := {}
  let mut axioms : NameSet := {}
  let mut todo := roots
  while !todo.isEmpty do
    let name := todo.back!
    todo := todo.pop
    if seen.contains name then continue
    seen := seen.insert name
    let some info := env.checked.get.find? name
      | throw s!"Unknown proof dependency: {name}"
    if info matches .axiomInfo _ then axioms := axioms.insert name
    todo := todo ++ info.type.getUsedConstants
    if let some value := info.value? (allowOpaque := true) then
      todo := todo ++ value.getUsedConstants
    if let .inductInfo value := info then
      todo := todo ++ value.ctors.toArray
  return (axioms.toArray.qsort Name.lt, seen.size)

def auditPair (env : Environment) (target expected : Name) : IO Unit := do
  let some (.thmInfo actual) := env.checked.get.find? target
    | throw <| IO.userError s!"Target is missing or is not a theorem: {target}"
  let some (.defnInfo canonical) := env.checked.get.find? expected
    | throw <| IO.userError s!"Expected proposition is missing or is not a definition: {expected}"
  unless actual.levelParams.isEmpty && canonical.levelParams.isEmpty do
    throw <| IO.userError "Unexpected universe parameters"
  match Kernel.isDefEq env {} actual.type canonical.value with
  | .ok true => IO.println s!"TYPE_MATCH {target} {expected}"
  | _ => throw <| IO.userError s!"Type comparison failed: {target} vs {expected}"

unsafe def main (args : List String) : IO UInt32 := do
  -- The four-argument form supports small native regression fixtures. The
  -- default checks all five public theorems against Mathlib-only propositions.
  let (moduleName, expectedModule, pairs) ← match args with
    | [mod, expMod, target, expected] => pure (mod.toName, expMod.toName, #[(target.toName, expected.toName)])
    | [] => pure (`Main, `ExpectedStatements, #[
        (`riemannHypothesis, `RHExpected.RHMathlib),
        (`riemannZeta_eq_zero_re, `RHExpected.RHLit),
        (`LFunction_ne_zero_of_half_lt_re, `RHExpected.DirichletNonvanishingLit),
        (`LFunction_eq_zero_re, `RHExpected.DirichletZerosLit),
        (`LFunction_eq_zero_re_of_isPrimitive, `RHExpected.PrimitiveDirichletZerosLit)])
    | _ => throw <| IO.userError "usage: [MODULE EXPECTED_MODULE TARGET EXPECTED_DEFINITION]"
  initSearchPath (← findSysroot)
  -- loadExts is always false in withImportModules. Only an exit code escapes
  -- this callback, so no references survive its compacted-region cleanup.
  withImportModules #[{module := expectedModule}, {module := moduleName}] {} fun env => do
    for (target, expected) in pairs do auditPair env target expected
    let (axioms, visited) ← match rawAxioms env (pairs.map Prod.fst) with
      | .ok result => pure result
      | .error error => throw <| IO.userError error
    IO.println s!"RAW_AXIOMS {axioms}; visited {visited} declarations"
    let allowed := #[`propext, `Classical.choice, `Quot.sound]
    for ax in axioms do
      unless allowed.contains ax do
        throw <| IO.userError s!"Unexpected axiom {ax}"
    IO.println s!"INDEPENDENT_AUDIT_PASSED {pairs.size} targets; {env.header.moduleNames.size} modules"
    return 0
