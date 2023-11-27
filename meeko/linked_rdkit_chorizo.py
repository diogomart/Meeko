import pathlib
import json
from rdkit import Chem
from rdkit.Chem import rdFMCS
from rdkit.Chem import rdChemReactions
from rdkit.Chem.AllChem import EmbedMolecule, AssignBondOrdersFromTemplate
from .writer import PDBQTWriterLegacy
from .molsetup import RDKitMoleculeSetup

from misctools import react_and_map

import numpy as np

pkg_dir = pathlib.Path(__file__).parents[0]
with open(pkg_dir / "data" / "prot_res_params.json") as f:
    chorizo_params = json.load(f)

def mapping_by_mcs(mol, ref):
    mcs_result = rdFMCS.FindMCS([mol,ref], bondCompare=rdFMCS.BondCompare.CompareAny)
    mcs_mol = Chem.MolFromSmarts(mcs_result.smartsString)

    mol_idxs = mol.GetSubstructMatch(mcs_mol)
    ref_idxs = ref.GetSubstructMatch(mcs_mol)

    atom_map = {i:j for (i,j) in zip(mol_idxs, ref_idxs)}
    return atom_map

def reassign_formal_charge(mol, ref, mapping):
    #TODO this could be optimized
    #TODO ref charges could be precalculated to speed up large structures
    mol_charged_atoms = []
    for idx, atom in enumerate(mol.GetAtoms()):
        if atom.GetFormalCharge() != 0:
            mol_charged_atoms.append(idx)
            
    ref_charged_atoms = []
    for idx, atom in enumerate(ref.GetAtoms()):
        if atom.GetFormalCharge() != 0:
            ref_charged_atoms.append(idx)
    
    for (k,v) in mapping.items():
        if k in ref_charged_atoms or v in mol_charged_atoms:
            mol.GetAtomWithIdx(v).SetFormalCharge(ref.GetAtomWithIdx(k).GetFormalCharge())

    return mol

def reassign_bond_orders(mol, ref, mapping):
    #TODO this could be optimized
    for i in mapping.keys():
        for ref_bond in ref.GetAtomWithIdx(i).GetBonds():
            j = ref_bond.GetOtherAtomIdx(i)
            if j in mapping.keys():
                mol_bond = mol.GetBondBetweenAtoms(mapping[i], mapping[j])
                mol_bond.SetBondType(ref_bond.GetBondType())
    return mol

def h_coord_from_dipeptide(pdb1, pdb2):
    mol = Chem.MolFromPDBBlock(pdb1+pdb2)
    mol_h = Chem.AddHs(mol, addCoords=True)
    ps = Chem.SmilesParserParams()
    ps.removeHs = False
    template = Chem.MolFromSmiles('C(=O)C([H])N([H])C(=O)C([H])N',ps)
    h_idx = 5
    atom_map = mapping_by_mcs(template, mol_h)
    
    return mol_h.GetConformer().GetAtomPosition(atom_map[h_idx])

def h_coord_random_n_terminal(mol, debug=False):
    mol_no_h = Chem.RemoveHs(mol)
    for atom in mol_no_h.GetAtoms():
        if atom.GetProp("atom_name") == "N":
            bb_n_idx = atom.GetIdx()
    mol_h = Chem.AddHs(mol_no_h, addCoords=True)
    #positions = mol_h.GetConformer().GetPositions()
    bb_n = mol_h.GetAtomWithIdx(bb_n_idx)
    for neighbor in bb_n.GetNeighbors():
        if neighbor.GetAtomicNum() == 1:
            return mol_h.GetConformer().GetAtomPosition(neighbor.GetIdx())


def _snap_to_int(value, tolerance=0.12):
    for inc in [-1, 0, 1]:
        if abs(value - int(value) - inc) <= tolerance:
            return int(value) + inc
    return None

def divide_int_gracefully(integer, weights, allow_equal_weights_to_differ=False):
    for weight in weights:
        if type(weight) not in [int, float] or weight < 0:
            raise ValueError("weights must be numeric and non-negative")
    if type(integer) != int:
        raise ValueError("integer must be integer")
    inv_total_weight = 1.0 / sum(weights)
    shares = [w * inv_total_weight for w in weights] # normalize
    result = [_snap_to_int(integer * s, tolerance=0.5) for s in shares]
    surplus = integer - sum(result)
    if surplus == 0:
        return result
    data = [(i, w) for (i, w) in enumerate(weights)]
    data = sorted(data, key=lambda x: x[1], reverse=True)
    idxs = [i for (i, _) in data] 
    if allow_equal_weights_to_differ:
        groups = [1 for _ in weights]
    else:
        groups = []
        last_weight = None
        for i in idxs:
            if weights[i] == last_weight:
                groups[-1] += 1
            else:
                groups.append(1)
            last_weight = weights[i]

    # iterate over all possible combinations of groups
    # this is potentially very slow
    nr_groups = len(groups)
    for j in range(1, 2**nr_groups):
        n_changes = 0
        combo = []
        for grpidx in range(nr_groups):
            is_changed = bool(j & 2**grpidx) 
            combo.append(is_changed)
            n_changes += is_changed * groups[grpidx]
        if n_changes == abs(surplus):
            break

    # add or subtract 1 to distribute surplus
    increment = surplus / abs(surplus)
    index = 0
    for i, is_changed in enumerate(combo):
        if is_changed:
            for j in range(groups[i]):
                result[idxs[index]] += increment
                index += 1

    return result

def rectify_charges(q_list, net_charge=None, decimals=3):
    """make them 3 decimals and sum to an integer"""

    fstr = "%%.%df" % decimals
    charges_dec = [float(fstr % q) for q in q_list]

    if net_charge is None:
        net_charge = _snap_to_int(sum(charges_dec), tolerance=0.15)
        if net_charge is None:
            msg = "net charge could not be predicted from input q_list. (residual is beyond tolerance) "
            msg = "Please set the net_charge argument directly"
            raise RuntimeError(msg)
    elif type(net_charge) != int:
        raise TypeError("net charge must be an integer")

    surplus = net_charge - sum(charges_dec)
    surplus_int = _snap_to_int(10**decimals * surplus)

    if surplus_int == 0:
        return charges_dec

    weights = [abs(q) for q in q_list]
    surplus_int_splits = divide_int_gracefully(surplus_int, weights)
    for i, increment in enumerate(surplus_int_splits):
        charges_dec[i] += 10**-decimals * increment

    return charges_dec

class LinkedRDKitChorizo:
    """
    This is a class used to take in information about a series of linked Chorizo residues
    and there are most likely many more details that should be included here.

    Attributes
    ----------
    cterm_pad_smiles: string
    nterm_pad_smiles: string
    backbone_smarts: string
    backbone: rdkit.Chem.rdchem.Mol
    backboneh: rdkit.Chem.rdchem.Mol
    nterm_pad_backbone_smarts_idxs: tuple of ints
    cterm_pad_backbone_smarts_idxs: tuple of ints
    rxn_cterm_pad: RDKit ChemicalReaction
    rxn_nterm_pad: RDKit ChemicalReaction

    residues: dict (string -> ChorizoResidue) #TODO: figure out exact SciPy standard for this
    """

    cterm_pad_smiles = "CN"
    nterm_pad_smiles = "CC=O"
    backbone_smarts = "[C:1](=[O:2])[C:3][N:4]" # TODO make sure it matches res only once
    backbone = Chem.MolFromSmarts(backbone_smarts)
    backboneh = Chem.MolFromSmarts("[C:1](=[O:2])[C:3][N:4][#1]")
    nterm_pad_backbone_smarts_idxs = (0, 2, 1)
    cterm_pad_backbone_smarts_idxs = (2, 3)
    rxn_cterm_pad = rdChemReactions.ReactionFromSmarts(f"[N:5][C:6].{backbone_smarts}>>[C:6][N:5]{backbone_smarts}")
    rxn_nterm_pad = rdChemReactions.ReactionFromSmarts(f"[C:5][C:6]=[O:7].{backbone_smarts}>>{backbone_smarts}[C:6](=[O:7])[C:5]")


    def __init__(self, pdb_path, params=chorizo_params, mutate_res_dict=None, termini=None, deleted_residues=None, allow_bad_res=False):
        suggested_mutations = {}
        
        # Generates the residue representations based purely on the information in the pdb file to begin with.
        self.residues = self._pdb_to_resblocks(pdb_path)
        res_list = self.residues.keys()
        
        self.termini = self._check_termini(termini, res_list)
        if deleted_residues is None:
            deleted_residues = ()
        self._check_del_res(deleted_residues, res_list)
        self.deleted_residues = deleted_residues
        
        self.mutate_res_dict = mutate_res_dict
        if mutate_res_dict is not None:
            self._rename_residues(mutate_res_dict)
        self.res_templates, self.ambiguous = self._load_params(params)

        ambiguous_chosen = self.parameterize_residues(self.termini, deleted_residues, self.ambiguous)
        suggested_mutations.update(ambiguous_chosen)

        removed_residues = self.getIgnoredResidues()
        if len(removed_residues) > 0 and not allow_bad_res:
            for res in removed_residues:
                suggested_mutations[res] = res
            print("The following mutations are suggested. For HIS, mutate to HID, HIE, or HIP.")
            print(json.dumps(suggested_mutations, indent=2))
            msg = "The following residues could not be processed:" + pathlib.os.linesep
            msg += self.print_residues_by_resname(removed_residues)
            raise RuntimeError(msg)

        self.disulfide_bridges = self._find_disulfide_bridges()
        for cys_1, cys_2 in self.disulfide_bridges:
            chain_1, resname_1, resnum_1 = cys_1.split(":")
            chain_2, resname_2, resnum_2 = cys_2.split(":")
            if resname_1 != "CYX" or resname_2 != "CYX":
                print(f"Likely disulfide bridge between {cys_1} and {cys_2}")
            if resname_1 != "CYX":
                cyx_1 = f"{chain_1}:CYX:{resnum_1}"
                suggested_mutations[cys_1] = cyx_1
                if (cys_1 not in mutate_res_dict) and ((cys_2 not in mutate_res_dict) or resname_2 == "CYX"):
                    self._rename_residues({cys_1: cyx_1})
                    resmol = self.build_resmol(cyx_1, "CYX")
                    if resmol is not None:
                        self.residues[cyx_1].rdkit_mol = resmol
                    else:
                        self.residues[cyx_1].ignore_residue = True
            if resname_2 != "CYX":
                cyx_2 = f"{chain_2}:CYX:{resnum_2}"
                suggested_mutations[cys_2] = cyx_2
                if (cys_2 not in mutate_res_dict) and ((cys_1 not in mutate_res_dict) or resname_1 == "CYX"):
                    self._rename_residues({cys_2: cyx_2})
                    resmol = self.build_resmol(cyx_2, "CYX")
                    if resmol is not None:
                        self.residues[cyx_2].rdkit_mol = resmol
                    else:
                        self.residues[cyx_2].ignore_residue = True

        """to_remove = []
        for res_id in self.getIgnoredResidues():
            i = self.res_list.index(res_id)
            to_remove.append(i)
        for i in sorted(to_remove, reverse=True):
            self.res_list.pop(i) """
        self.suggested_mutations = suggested_mutations
        return

    def _find_disulfide_bridges(self):
        cys_list = {}
        cutoff = 2.5 # angstrom
        bridges = []
        for res in self.residues:
            if self.residues[res].ignore_residue: continue
            resname = res.split(":")[1]
            if resname in ["CYS", "CYX", "CYM"]: # TODO move "protected resnames" next to residue params they are associated with
                resmol = self.residues[res].rdkit_mol
                molxyz = resmol.GetConformer().GetPositions()
                s_xyz = None
                for atom in resmol.GetAtoms():
                    if atom.GetAtomicNum() == 16:
                        s_xyz = molxyz[atom.GetIdx()]
                for cys, other_s_xyz in cys_list.items():
                    v = s_xyz - other_s_xyz
                    dist = np.sqrt(np.dot(v, v))
                    if dist < cutoff:
                        bridges.append((res, cys))
                cys_list[res] = s_xyz
        return bridges

    
    @staticmethod
    def _check_del_res(query_res, existing_res):
        missing = set()
        for res in query_res:
            if res not in existing_res:
                missing.add(res)
        if len(missing) > 0:
            msg = "deleted_residues not found: " + " ".join(missing)
            raise ValueError(msg)


    @staticmethod
    def _check_termini(termini, res_list):
        allowed_c = ("cterm", "c-term", "c")
        allowed_n = ("nterm", "n-term", "n")
        output = {}
        if termini is None:
            return output
        for (resn, value) in termini.items():
            if resn not in res_list:
                raise ValueError("%s in termini not found" % resn)
            output[resn] = []
            if value.lower() in allowed_c:
                output[resn] = "C"
            elif value.lower() in allowed_n:
                output[resn] = "N"
            else:
                raise ValueError("termini value was %s, expected %s or %s" % (value, allowed_c, allowed_n))
        return output


    def get_padded_mol(self, resn):
        #TODO disulfides, ACE, NME
        # TODO double check next/previous res logic for "blunt" ending
        def _join(mol, pad_mol, pad_smarts_mol, rxn, is_res_atom, mapidx, adjacent_mol=None, pad_smarts_idxs=None):
            pad_matches = adjacent_mol.GetSubstructMatches(pad_smarts_mol)
            if len(pad_matches) != 1:
                raise RuntimeError("expected 1 match but got %d" % (len(pad_matches)))
            conformer = Chem.Conformer(pad_mol.GetNumAtoms())
            pad_mol.AddConformer(conformer)
            if adjacent_mol is not None:
                for index, smarts_index in enumerate(pad_smarts_idxs):
                    adjacent_mol_index = pad_matches[0][smarts_index]
                    pos = adjacent_mol.GetConformer().GetAtomPosition(adjacent_mol_index)
                    pad_mol.GetConformer().SetAtomPosition(index, pos)
            products, index_map = react_and_map((pad_mol, mol), rxn)
            if len(products) != 1:
                raise RuntimeError("expected 1 reaction product but got %d" % (len(products)))
            mol = products[0][0]
            index_map["reactant_idx"] = index_map["reactant_idx"][0][0]
            index_map["atom_idx"] = index_map["atom_idx"][0][0]
            Chem.SanitizeMol(mol)
            new_is_res_atom = []
            new_mapidx = {}
            for atom in mol.GetAtoms():
                index = atom.GetIdx()
                reactant_idx = index_map["reactant_idx"][index]
                if  reactant_idx == 0:
                    new_is_res_atom.append(False)
                elif reactant_idx == 1: # mol is 2nd reactant (0-index)
                    atom_idx = index_map["atom_idx"][index] 
                    new_is_res_atom.append(is_res_atom[atom_idx])
                    if atom_idx in mapidx:
                        new_mapidx[index] = mapidx[atom_idx]
                else:
                    raise RuntimeError("we have only two reactants, got %d ?" % reactant_idx)
            return mol, new_is_res_atom, new_mapidx

        mol = Chem.Mol(self.residues[resn].rdkit_mol)
        is_res_atom = [True for atom in mol.GetAtoms()]
        mapidx = {atom.GetIdx(): atom.GetIdx() for atom in mol.GetAtoms()}
        if self.residues[resn].previous_id is not None:
            prev_resn = self.residues[resn].previous_id
            prev_mol = self.residues[prev_resn].rdkit_mol
            nterm_pad = Chem.MolFromSmiles(self.nterm_pad_smiles)
            mol, is_res_atom, mapidx = _join(
                    mol,
                    nterm_pad,
                    self.backbone,
                    self.rxn_nterm_pad,
                    is_res_atom,
                    mapidx,
                    prev_mol,
                    self.nterm_pad_backbone_smarts_idxs)
            
        if self.residues[resn].next_id is not None:
            next_resn = self.residues[resn].next_id
            next_mol = self.residues[next_resn].rdkit_mol
            cterm_pad = Chem.MolFromSmiles(self.cterm_pad_smiles)
            mol, is_res_atom, mapidx = _join(
                    mol,
                    cterm_pad,
                    self.backbone,
                    self.rxn_cterm_pad,
                    is_res_atom,
                    mapidx,
                    next_mol, 
                    self.cterm_pad_backbone_smarts_idxs)
        n_atoms_before_addhs = mol.GetNumAtoms()
        mol = Chem.AddHs(mol)
        is_res_atom.extend([False] * (mol.GetNumAtoms() - n_atoms_before_addhs))
        return mol, is_res_atom, mapidx

    def res_to_molsetup(self, res, mk_prep, is_protein_sidechain=False, cut_at_calpha=False):
        padded_mol, is_res_atom, mapidx = self.get_padded_mol(res)
        if is_protein_sidechain:
            bb_matches = padded_mol.GetSubstructMatches(self.backboneh)
            if len(bb_matches) != 1:
                raise RuntimeError("expected 1 backbone match, got %d" % (len(bb_matches)))
            c_alpha = bb_matches[0][2]
        else:
            c_alpha = None
        molsetups = mk_prep.prepare(padded_mol, root_atom_index=c_alpha)
        if len(molsetups) > 1:
            raise NotImplementedError("multiple molsetups not yet implemented for flexres")
        molsetup = molsetups[0]
        ignored_in_molsetup = []
        for atom_index in molsetup.atom_ignore:
            if atom_index < len(is_res_atom):
                is_res = is_res_atom[atom_index] # Hs from Chem.AddHs beyond length of is_res_atom
            else:
                is_res = False
            ignore = not is_res
            ignore |= is_protein_sidechain and cut_at_calpha and (
                    (atom_index != c_alpha) and (atom_index in bb_matches[0]))
            molsetup.atom_ignore[atom_index] |= ignore
            if ignore and is_res:
                ignored_in_molsetup.append(mapidx[atom_index])
        # rectify charges to sum to integer (because of padding)
        net_charge = sum([atom.GetFormalCharge() for atom in self.residues[res].rdkit_mol.GetAtoms()])
        not_ignored_idxs = []
        charges = []
        for i, q in molsetup.charge.items(): # charge is ordered dict
            if i in mapidx:
                charges.append(q)
                not_ignored_idxs.append(i) 
        charges = rectify_charges(charges, net_charge, decimals=3)
        for i, j in enumerate(not_ignored_idxs):
            molsetup.charge[j] = charges[i]
        return molsetup, mapidx, ignored_in_molsetup


    def flexibilize_protein_sidechain(self, res, mk_prep, cut_at_calpha=False):
        molsetup, mapidx, ignored_in_molsetup = self.res_to_molsetup(res, mk_prep,
                                                                  is_protein_sidechain=True,
                                                                  cut_at_calpha=cut_at_calpha)
        self.residues[res].molsetup = molsetup
        self.residues[res].molsetup_mapidx = mapidx
        self.residues[res].molsetup_ignored = ignored_in_molsetup
        self.residues.is_movable = True
        return

    def add_molsetup_inflexible(self, res, mk_prep, cut_at_calpha=False):
        molsetup, mapidx, ignored_in_molsetup = self.res_to_molsetup(res, mk_prep,
                                                                  is_protein_sidechain=False,
                                                                  cut_at_calpha=cut_at_calpha)  
        self.residues[res].molsetup = molsetup
        self.residues[res].molsetup_mapidx = mapidx
        self.residues[res].molsetup_ignored = ignored_in_molsetup
        self.residues.is_movable = False
        return

    @staticmethod
    def print_residues_by_resname(removed_residues):
        by_resname = dict()
        for res_id in removed_residues:
            chain, resn, resi = res_id.split(":")
            by_resname.setdefault(resn, [])
            by_resname[resn].append(f"{chain}:{resi}")
        string = ""
        for resname, removed_res in by_resname.items():
            string += f"Resname: {resname}:" + pathlib.os.linesep
            string += " ".join(removed_res) + pathlib.os.linesep
        return string

    @staticmethod 
    def _load_params(params):
        undesired_props = ("bonds", "//", "bond_cut_atoms", "smiles")
        
        ps = Chem.SmilesParserParams()
        ps.removeHs = False

        res_templates = {}
        for resn in params:
            if resn == "ambiguous": continue
            resmol = Chem.MolFromSmiles(params[resn]['smiles'], ps)
            # Using user provided parameters to assign properties to atoms.
            for idx, atom in enumerate(resmol.GetAtoms()):
                for propname in params[resn]:
                    if propname in undesired_props:
                        continue
                    value = params[resn][propname][idx]
                    if value is None:
                        continue
                    if type(value) == bool:
                        atom.SetBoolProp(propname, value)
                    elif type(value) == float:
                        atom.SetDoubleProp(propname, value)
                    elif type(value) == str:
                        atom.SetProp(propname, value)
                    else:
                        raise RuntimeError("property type:", type(value), value, "propname:", propname, "resn:", resn)
            res_templates[resn] = resmol
        ambiguous = params["ambiguous"]
        return res_templates, ambiguous
    
    @staticmethod
    def _pdb_to_resblocks(pdb_path):
        residues = {}
        with open(pdb_path, 'r') as fin:
            # Tracking the key and the value for the current dictionary pair being read in
            current_res_id = None # the residue id we are tracking
            current_res = None    # the ChorizoResidue object we are tracking
            for line in fin:
                if line.startswith('TER') and current_res is not None:
                    current_res.next_id = None
                    residues[current_res_id] = current_res
                    current_res = None
                    current_res_id = None
                if line.startswith('ATOM') or line.startswith('HETATM'):
                    # Generating dictionary key
                    resname = line[17:20].strip()
                    resid = int(line[22:26].strip())
                    chainid = line[21].strip()
                    full_res_id = ':'.join([chainid, resname, str(resid)])

                    if full_res_id == current_res_id:
                        current_res.pdb_text += line
                    else:
                        if current_res is not None:
                            current_res.next_id = full_res_id
                            residues[current_res_id] = current_res
                        # Updates tracking to the new key-value pair we're dealing with
                        current_res = ChorizoResidue(full_res_id, line)
                        current_res.previous_id = current_res_id
                        current_res_id = full_res_id
            if current_res is not None:
                current_res.next_id = None
                residues[current_res_id] = current_res
        return residues
    
    def _rename_residues(self, mutate_dict):
        for res in mutate_dict:
            old_resn = res.split(':')[1]
            new_resn = mutate_dict[res].split(':')[1]
            old_residue = self.residues[mutate_dict[res]]
            self.residues[mutate_dict[res]] = self.residues[res]
            # self.residues[mutate_dict[res]] = self.residues.pop(res)
            # self.residues[mutate_dict[res]].['pdb block'] = self.residues[mutate_dict[res]]['pdb block'].replace(old_resn, new_resn)
            previous_res = self.residues[mutate_dict[res]].previous_id
            if previous_res:
                self.residues[previous_res].next_id = mutate_dict[res]
            next_res = self.residues[mutate_dict[res]].next_id
            if next_res:
                self.residues[next_res].previous_id = mutate_dict[res]

            """i = self.res_list.index(res)
            self.res_list[i] = mutate_dict[res]"""

    @staticmethod
    def add_termini(resn, res, termini, residues):
        # resn, res, termini, self.residues -> input
        next_res = residues[res].next_id
        prev_res = residues[res].previous_id
        if termini.get(res, None) == "C":
            if (next_res is not None) and (not residues[next_res].user_deleted):
                raise ValueError("Trying to C-term {res} but {next_res=} exists")
            resn = 'C' + resn
        elif termini.get(res, None) == "N":
            if (prev_res is not None) and (not residues[prev_res].user_deleted):
                raise ValueError("Trying to N-term {res} but {prev_res=} exists")
            resn = 'N' + resn
        elif termini.get(res, None) is None:
            resn = resn # wow, such assignment, very code
        else:
            # TODO verify sanity of termini earlier
            raise ValueError("termini must be either 'C' or 'N', not %s" % termini.get(res, None))
        return resn

    def parameterize_residues(self, termini, deleted_residues, ambiguous):
        ambiguous_chosen = {}
        for res in self.residues:
            # skip deleted resides
            if res in deleted_residues:
                continue
            
            # if we can't generate an RDKit Mol for the residue, mark it as ignored and skip
            pdbmol = Chem.MolFromPDBBlock(self.residues[res].pdb_text, removeHs=False)
            if pdbmol is None:
                self.residues[res].ignore_residue = True
                continue
            
            # Check if parameters are available for a residue
            chain, resn, resnum = res.split(':')
            if (resn not in self.res_templates) and (resn not in ambiguous):
                #self.residues.pop(res)
                self.residues[res].ignore_residue = True
                continue

            if resn in ambiguous:
                possible_resn = ambiguous[resn]
            else:
                possible_resn = [resn]

            #if resn == "HIS": print("HIS ambiguous:", possible_resn)

            lowest_nr_missing = 9999999
            # loops through possible resmol generated for the residue based on number of atoms missing
            for resn in possible_resn:

                resn = self.add_termini(resn, res, termini, self.residues) # prefix C or N if applicable

                # TODO add to preprocessing to save time
                # Create mol object and map between the pdb and residue template
                resmol = Chem.Mol(self.res_templates[resn])
                n_atoms = len(resmol.GetAtoms())
                atom_map = mapping_by_mcs(resmol, pdbmol)
                nr_missing = n_atoms - len(atom_map)
                if nr_missing < lowest_nr_missing:
                    best_resmol = resmol
                    lowest_nr_missing = nr_missing
                    best_n_atoms = n_atoms
                    best_resn = resn
            resmol = best_resmol
            n_atoms = best_n_atoms
            resn = best_resn
            if len(possible_resn) > 1:
                print("%9s" % res, "-->", resn, "...out of", possible_resn)
                ambiguous_chosen[res] = f"{chain}:{resn}:{resnum}"

            resmol = self.build_resmol(res, resn)
            if resmol is None:
                self.residues[res].ignore_residue = True
            else:
                self.residues[res].rdkit_mol = resmol
        return ambiguous_chosen

    def build_resmol(self, res, resn):
        # Transfer coordinates and info for any matched atoms
        #TODO time these functions
        #TODO maybe embed in preprocessing depending on time
        #EmbedMolecule(resmol)

        resmol = Chem.Mol(self.res_templates[resn])
        pdbmol = Chem.MolFromPDBBlock(self.residues[res].pdb_text, removeHs=False)

        atom_map = mapping_by_mcs(resmol, pdbmol)
        #Chem.rdDepictor.Compute2DCoords(resmol)
        resmol.AddConformer(Chem.Conformer(resmol.GetNumAtoms()))

        resmol.GetConformer().Set3D(True)
        for idx, pdb_idx in atom_map.items():
            pdb_atom = pdbmol.GetAtomWithIdx(pdb_idx)
            pdb_coord = pdbmol.GetConformer().GetAtomPosition(pdb_idx)
            resmol.GetConformer().SetAtomPosition(idx, pdb_coord)

            resinfo = pdb_atom.GetPDBResidueInfo()
            resmol.GetAtomWithIdx(idx).SetDoubleProp('occupancy', resinfo.GetOccupancy())
            resmol.GetAtomWithIdx(idx).SetDoubleProp('temp_factor', resinfo.GetTempFactor())

        missing_atoms = {resmol.GetAtomWithIdx(i).GetProp('atom_name'):i for i in range(resmol.GetNumAtoms()) if i not in atom_map.keys()}

        # Handle case of missing backbone amide H
        if 'H' in missing_atoms:
            prev_res = self.residues[res].previous_id
            if prev_res is not None:
                h_pos = h_coord_from_dipeptide(self.residues[res].pdb_text, 
                                                self.residues[prev_res].pdb_text)
            else:
                h_pos = h_coord_random_n_terminal(resmol)
            resmol.GetConformer().SetAtomPosition(missing_atoms['H'], h_pos)
            resmol.GetAtomWithIdx(missing_atoms['H']).SetBoolProp('computed', True)
            missing_atoms.pop('H')
            
        
        missing_atom_elements = set([atom[0] for atom in missing_atoms.keys()])
        if len(missing_atom_elements) > 0:
            
            resmol_h = Chem.RemoveHs(resmol)
            resmol_h = Chem.AddHs(resmol_h, addCoords=True)
            h_map = mapping_by_mcs(resmol, resmol_h)
            for atom in list(missing_atoms.keys()):
                if atom.startswith('H'):
                    h_idx = missing_atoms[atom]
                    #TODO magic function from Diogo to add H atom
                    #TODO bring in parameterless H atom for chain breaks
                    resmol.GetConformer().SetAtomPosition(h_idx, resmol_h.GetConformer().GetAtomPosition(h_map[h_idx]))
                    resmol.GetAtomWithIdx(h_idx).SetBoolProp('computed', True)
                    missing_atoms.pop(atom)
        # TODO missing atoms from PDB? Extra PDB atoms OK currently?
        if len(missing_atoms) > 0:
            err = f'Could not add {res=} {missing_atoms=}'
            print(err)
            resmol = None
        return resmol


    def mk_parameterize_all_residues(self, mk_prep):
        # TODO disulfide bridges are hard-coded, generalize branching maybe
        for res in self.residues:
            if self.residues[res].user_deleted or self.residues[res].ignore_residue:
                continue
            self.mk_parameterize_residue(self, res, mk_prep)
        return


    def mk_parameterize_residue(self, res, mk_prep): 
        molsetup, mapidx, ignored_in_molsetup = self.res_to_molsetup(res, mk_prep)
        resmol = self.residues[res].rdkit_mol
        for molsetup_idx, resmol_idx in mapidx.items(): # ignoring pseudo atoms, rdkit atom props no good for pseudos
            atom = resmol.GetAtomWithIdx(resmol_idx)
            atom.SetDoubleProp("q", molsetup.charge[molsetup_idx])
            atom.SetProp("atom_type", molsetup.atom_type[molsetup_idx])
            #atom.SetProp("ignore", molsetup.atom_ignore[molsetup_idx])
            for key, value_array in molsetup.atom_params.items():
                value = value_array[molsetup_idx]
                if type(value) == float:
                    atom.SetDoubleProp(key, value)
                elif type(value) == bool:
                    atom.SetBoolProp(key, value)
                else:
                    atom.SetProp(key, value)
        # consider deleting existing properties/parameters
        return
                

    def write_pdb(self, outpath):
        # TODO currently does not contain residue information
        with open(outpath, 'w') as fout:
            for res in self.residues:
                try:
                    pdb_block = Chem.MolToPDBBlock(self.residues[res].rdkit_mol)
                except:
                    print(res)
                for line in pdb_block.split('\n'):
                    if line.startswith('HETATM'):
                        fout.write(line+'\n')

    def export_static_atom_params(self, ignore_atom_types=("H",)):
        atom_params = {}
        counter_atoms = 0
        coords = []
        for res_id in self.residues:
            if self.residues[res_id].user_deleted:
                continue
            resmol = self.residues[res_id].rdkit_mol
            for atom in resmol.GetAtoms():
                props = atom.GetPropsAsDict()
                if len(ignore_atom_types) > 0 and props["atom_type"] in ignore_atom_types:
                    continue
                if (self.residues[res_id].molsetup) and (
                    atom.GetIdx() not in self.residues["molsetup_ignored"]):
                        continue
                for key, value in props.items():
                    if key.startswith("_"):
                        continue
                    atom_params.setdefault(key, [None]*counter_atoms) # add new "column"
                    atom_params[key].append(value)
                counter_atoms += 1
                for key in set(atom_params).difference(props): # <key> missing in <props>
                    atom_params[key].append(None) # fill in incomplete "row"
                coords.append(resmol.GetConformer().GetAtomPosition(atom.GetIdx()))
        if hasattr(self, "param_rename"): # e.g. "gasteiger" -> "q"
            for key, new_key in self.param_rename.items():
                atom_params[new_key] = atom_params.pop(key)
        return atom_params, coords
    
    # The following functions return filtered dictionaries of residues based on the value of residue flags.
    def getUserDeletedResidues(self):
        return {k: v for k, v in self.residues.items() if v.user_deleted == True}
        
    def getNonUserDeletedResidues(self):
        return {k: v for k, v in self.residues.items() if v.user_deleted == False}
        
    def getIgnoredResidues(self):
        return {k: v for k, v in self.residues.items() if v.ignore_residue == True}
        
    def getNotIgnoredResidues(self):
        return {k: v for k, v in self.residues.items() if v.ignore_residue == False}

class ChorizoResidue:
    """
    A class representing a single residue in the chain of chorizo residues

    Attributes
    ----------
    residue_id: string
        the residue id, most likely the key for this object if it is being 
        stored in a dictionary
    pdb_text: string
        the text from the pdb file associated with this residue
    previous_id: string or None
        the previous residue in this chain
    next_id: string or None
        the next residue in this chain
    
    rdkit_mol: rdkit.Chem.rdchem.Mol or None
        the rdkit Mol object generated from this residue
    molsetup: RDKitMoleculeSetup or None
        the RDKitMoleculeSetup object generated from this residue
    molsetup_mapidx: or None
        needs info
    molsetup_ignored: List[] or None
        needs info

    ignore_residue: bool
        marks residues that formerly were part of the removed_residues structure,
        put on residues that are being ignored 
    is_movable: bool
        marks residues that are flexible
    user_deleted: bool
        marks residues that the user indicated should be deleted

    additional_connections: List[ResidueAdditionalConnection]
        connections to additional residues along the chain (there's probably
        more specific information about the nature of these connections that
        could be added here)
    """
    def __init__(self, residue_id, pdb_text, previous_id=None, next_id=None):
        self.residue_id = residue_id
        self.pdb_text = pdb_text
        self.previous_id = previous_id
        self.next_id = next_id

        self.rdkit_mol = None
        self.molsetup = None
        self.molsetup_mapidx = None
        self.molsetup_ignored = None # Check about these data types/Do we want the default to be None or empty

        # flags
        self.ignore_residue = False
        self.is_movable = False
        self.user_deleted = False

        self.additional_connections = []

    def toJson(self):
        return json.dumps(self, default=lambda o: o.__dict__)

# This could be a named tuple or a dataclass as it stands, but that is dependent on the amount of custom behavior
# we want to encode for these additional connections.
class ResidueAdditionalConnection:
    """
    Represents additional connections & bonds from a residue

    Attributes
    ----------
    connection_residue: string
        the id of the connected residue
    connection_atom: string
        the connected atom
    bond_order: string                    # SHOULD MAYBE NOT BE A STRING?
        the bond order of the connection
    """
    def __init__(self, residue, atom, bond_order):
        self.connection_residue = None
        self.connection_atom = None
        self.bond_order = None

    def toJson(self):
        return json.dumps(self, default=lambda o: o.__dict__)
