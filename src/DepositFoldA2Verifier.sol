// SPDX-License-Identifier: GPL-3.0
/*
    Copyright 2021 0KIMS association.

    This file is generated with [snarkJS](https://github.com/iden3/snarkjs).

    snarkJS is a free software: you can redistribute it and/or modify it
    under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    snarkJS is distributed in the hope that it will be useful, but WITHOUT
    ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
    or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public
    License for more details.

    You should have received a copy of the GNU General Public License
    along with snarkJS. If not, see <https://www.gnu.org/licenses/>.
*/

pragma solidity >=0.7.0 <0.9.0;

contract DepositFoldA2Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 20901312453931986983070371975751072566398443441960436590636518297529489694139;
    uint256 constant alphay  = 21697131480926335546036040991413011755153140230317664815989518148158724370212;
    uint256 constant betax1  = 4869487231785253308212429570614741443191402866210819877611793555879218415484;
    uint256 constant betax2  = 8494223465375928023871052138064538434365863294294498107508764578998413175191;
    uint256 constant betay1  = 13692213518456195651330939167802977105042737180923530430942149449680303886878;
    uint256 constant betay2  = 6329045905833941605730059747255744716806210507261219104759569890353192015645;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 2539781220919709721486586805344725231509744971161776345647072856565726594283;
    uint256 constant deltax2 = 7749796635925972690966600557106114509406614845544370597988339357424950534294;
    uint256 constant deltay1 = 19058997433754801171098731449686330452451432285455867592032791990287126977819;
    uint256 constant deltay2 = 11673828952380169222056667306954639394283499134941556224237983185107737526219;

    
    uint256 constant IC0x = 12337010875526911864514219578836220441499574948889789579521931266342940107376;
    uint256 constant IC0y = 3662295480626563957395683136631034727496160078193623994354364183347033533055;
    
    uint256 constant IC1x = 21133776926455964405868801121936141366787349736059361090258281736829940555631;
    uint256 constant IC1y = 16153653888886261632457746958243406336521973301159948331638788934683394688305;
    
    uint256 constant IC2x = 2028914910512124133213097351021872539547497038467654389343774300483254530914;
    uint256 constant IC2y = 18770113308476409409603205471483756825012079605829429135217904805753179109664;
    
    uint256 constant IC3x = 20888089079493948474155868628172073767459650055074370762712781377744295126626;
    uint256 constant IC3y = 20308377951656687140986001644420390360194206726957733669335072652071480606165;
    
    uint256 constant IC4x = 12038879121757731116160099655254566740153724192836858184328313952625148295740;
    uint256 constant IC4y = 11398884516083762620843031024793338347894285190251397684334682900903822864159;
    
    uint256 constant IC5x = 2169306494682085997367776246134737694943110373589583565791304883083450698749;
    uint256 constant IC5y = 1410533342383486719805247068778200964245917512513172596290549792236032084888;
    
    uint256 constant IC6x = 14625540685390664666539213464337305112114895031061492612085959683066404698973;
    uint256 constant IC6y = 18050368409306249124025884188440088209222984751219538446369420753290742325570;
    
    uint256 constant IC7x = 18941907488111854646172139380325728403349291424673508256366190432389076097939;
    uint256 constant IC7y = 5216895132521447235288078151098487808447560018832319010083723992630283939240;
    
    uint256 constant IC8x = 9465915619942755981330547081725280236317383920961590869395919278834267447;
    uint256 constant IC8y = 8655143521695791779981299471404150967176608210431208755980789930351711685707;
    
    uint256 constant IC9x = 3905301328997983740976177730345673896011127590852994411920964391241157910252;
    uint256 constant IC9y = 6042319487759909186759529230350192662540523982763402758326887447881162732947;
    
    uint256 constant IC10x = 5127787542419205847350504231248750899979794473026979929075577392472222194986;
    uint256 constant IC10y = 3243273521317360438163701329198692445503523438841618478817175106185386609810;
    
    uint256 constant IC11x = 6263707091101470750155832336512908930729468643649295634920048866260306227971;
    uint256 constant IC11y = 21811453248358616491633142756839587049931624781880778753170378676253274219423;
    
    uint256 constant IC12x = 20057181045941364811342517014311094977428486664789719362776796346424177974728;
    uint256 constant IC12y = 5726943757019079968118878967893124160441129992003592767001036333429274379804;
    
    uint256 constant IC13x = 2028330651613021617657314429124948527518259268817787415556040187395230408537;
    uint256 constant IC13y = 420149482120958780119667450159972414481244613732453000459780728293214292147;
    
    uint256 constant IC14x = 20239686661571838464497655099447778103650943457750428302525697796367264005006;
    uint256 constant IC14y = 16268641786537458829848769857750777296333798357378201843712847361505772839485;
    
    uint256 constant IC15x = 5781275032133990418100683934024073743829453722006473982902803529048652572601;
    uint256 constant IC15y = 5196102443335777237945385671898894610768396771378478437940749883600553296161;
    
    uint256 constant IC16x = 17451247475898127623296685791980423816423663010597134784003554205005256625976;
    uint256 constant IC16y = 17715113158258809979300837680123797418827981621696342271602599176953983355213;
    
    uint256 constant IC17x = 797749080718781044231459397390984650429676762994189778116250733409762914214;
    uint256 constant IC17y = 12516116852733443372466637120060426193651573085470374642453076413296437442083;
    
    uint256 constant IC18x = 9751349723604031187695912543751585053975495462595750505010570135245600886855;
    uint256 constant IC18y = 1656917809774476846723904541732606765933151712764962249660075158908127166913;
    
    uint256 constant IC19x = 8646030748188293995437542443032488556092777951886954308488447420145627862903;
    uint256 constant IC19y = 2369675742359980706367556559032018516614511371504827982757539956927283644768;
    
    uint256 constant IC20x = 16853605309943554636790296836045634808539246879110609960145759225348099104365;
    uint256 constant IC20y = 5924582713098813610954825314179081973359522834509116280314230528596562302761;
    
    uint256 constant IC21x = 11266977699775344917784831743192559304956867209266130297763855897261224109531;
    uint256 constant IC21y = 11072288569944122898872337474038164749996734064367162368984590122036991912839;
    
    uint256 constant IC22x = 13310537260758922058817308211062591104908880202253439439712387593516146623072;
    uint256 constant IC22y = 16958865988700291649807176611890947729049755070590889533875671612600954776467;
    
    uint256 constant IC23x = 5680669167525351955171174729552617597127670925240709623389101023297110020473;
    uint256 constant IC23y = 9513885218331803390111647683366202470249659280577705314706393863911280929870;
    
    uint256 constant IC24x = 11688427456015505387585223514045147692735376878116658516032029750329478417382;
    uint256 constant IC24y = 17451996437271935819936025589938556516287561269904809096592101420306583147453;
    
    uint256 constant IC25x = 15386300068620880102510755044550963917600110123438800792953355740957806407280;
    uint256 constant IC25y = 11332334139315162010700350098015416124199897723430186308374936853466089290024;
    
    uint256 constant IC26x = 2252844751525086414682022257504202200090332838385126052361662602485133913457;
    uint256 constant IC26y = 13110333856809990270861912204332629757477110199364266312202969283863755300751;
    
    uint256 constant IC27x = 341022966715800058722984787079814669399300610676599406020649443631682611176;
    uint256 constant IC27y = 14211281357501899958493622649808864028089179472520891145696038962274909854299;
    
    uint256 constant IC28x = 10913363579713426218134183836687741018685330691434098390788557073059055513912;
    uint256 constant IC28y = 84527006631236155417051677310529751401873967649355295482897763505707508488;
    
    uint256 constant IC29x = 19968486056753835638336187073462119940140898576858975732991991649432041088491;
    uint256 constant IC29y = 13247445989465046146502863149864248456373420106893368343439912423633950166493;
    
    uint256 constant IC30x = 19655292693712118143408567527575860888095469614341672677124597509331165352267;
    uint256 constant IC30y = 21197958218000886543557739388260807762582139938128284217130440003493834223023;
    
    uint256 constant IC31x = 11959383415497870656043348470073852045443403105678710490877594357950895466192;
    uint256 constant IC31y = 21160147670195998270029589031774683995602920152072245306009945568053507933549;
    
    uint256 constant IC32x = 7161009877269603549005858642698582912648361694165657355108667799405651835523;
    uint256 constant IC32y = 13796246014680130474052262508015273715273880978505831110972027349436267235327;
    
    uint256 constant IC33x = 17503786603571871095341466354217625414449574982830041856142678298110142818501;
    uint256 constant IC33y = 7739653313718361005443049153357602691908036246245110807782106660668271626796;
    
    uint256 constant IC34x = 13894113014976213276174657260085123796196546773120045186680600556323203266686;
    uint256 constant IC34y = 9294012319303732503405239859612055048301871247766493761033071451329893912420;
    
    uint256 constant IC35x = 21681518329491537890109329269120166426616715114197399467164111755289368408862;
    uint256 constant IC35y = 18722653944522200313190386324805880865521555249132911444682931154549814997509;
    
    uint256 constant IC36x = 7903999628039063625274454334048307571731919974989644585986567727595148928465;
    uint256 constant IC36y = 4629011127077790107902667440429419585176858031149779407663048069595952311846;
    
    uint256 constant IC37x = 6975756631705126349457397774035002657905219605238342900806886837320820903359;
    uint256 constant IC37y = 20013974293824536816507046867917470706618632578871701636403161925846871011871;
    
    uint256 constant IC38x = 5221150791917357608606064001716034475763064413440808963011483852175090470617;
    uint256 constant IC38y = 12716548630239740728425881276442614227977021228836917117445246068024950072716;
    
    uint256 constant IC39x = 11607905193941652047114946092894833678092375648465420793588412874078676240701;
    uint256 constant IC39y = 14686316632979930206257525106951183036692520950878858064476448383320635789614;
    
    uint256 constant IC40x = 6734554387163171168170042690860952540712463488409145567266825313058845226825;
    uint256 constant IC40y = 20013606346079208080139356350352977458805744089787998739976570585179591145573;
    
    uint256 constant IC41x = 5241839575655787974565457095315724629846401057211449365818805951065346481359;
    uint256 constant IC41y = 3000341245833873624519725402752822820637748674154206593248866153121704748375;
    
    uint256 constant IC42x = 12658988254510719809694565178254571938677393999429821552303649121409644703460;
    uint256 constant IC42y = 1309507971296141583119178748446621551121339060777449191708889513038572634836;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[42] calldata _pubSignals) public returns (bool) {
        assembly {
            function checkField(v) {
                if iszero(lt(v, r)) {
                    mstore(0, 0)
                    return(0, 0x20)
                }
            }
            
            // G1 function to multiply a G1 value(x,y) to value in an address
            function g1_mulAccC(pR, x, y, s) {
                let success
                let mIn := mload(0x40)
                mstore(mIn, x)
                mstore(add(mIn, 32), y)
                mstore(add(mIn, 64), s)

                success := staticcall(sub(gas(), 2000), 7, mIn, 96, mIn, 64)

                if iszero(success) {
                    mstore(0, 0)
                    return(0, 0x20)
                }

                mstore(add(mIn, 64), mload(pR))
                mstore(add(mIn, 96), mload(add(pR, 32)))

                success := staticcall(sub(gas(), 2000), 6, mIn, 128, pR, 64)

                if iszero(success) {
                    mstore(0, 0)
                    return(0, 0x20)
                }
            }

            function checkPairing(pA, pB, pC, pubSignals, pMem) -> isOk {
                let _pPairing := add(pMem, pPairing)
                let _pVk := add(pMem, pVk)

                mstore(_pVk, IC0x)
                mstore(add(_pVk, 32), IC0y)

                // Compute the linear combination vk_x
                
                g1_mulAccC(_pVk, IC1x, IC1y, calldataload(add(pubSignals, 0)))
                
                g1_mulAccC(_pVk, IC2x, IC2y, calldataload(add(pubSignals, 32)))
                
                g1_mulAccC(_pVk, IC3x, IC3y, calldataload(add(pubSignals, 64)))
                
                g1_mulAccC(_pVk, IC4x, IC4y, calldataload(add(pubSignals, 96)))
                
                g1_mulAccC(_pVk, IC5x, IC5y, calldataload(add(pubSignals, 128)))
                
                g1_mulAccC(_pVk, IC6x, IC6y, calldataload(add(pubSignals, 160)))
                
                g1_mulAccC(_pVk, IC7x, IC7y, calldataload(add(pubSignals, 192)))
                
                g1_mulAccC(_pVk, IC8x, IC8y, calldataload(add(pubSignals, 224)))
                
                g1_mulAccC(_pVk, IC9x, IC9y, calldataload(add(pubSignals, 256)))
                
                g1_mulAccC(_pVk, IC10x, IC10y, calldataload(add(pubSignals, 288)))
                
                g1_mulAccC(_pVk, IC11x, IC11y, calldataload(add(pubSignals, 320)))
                
                g1_mulAccC(_pVk, IC12x, IC12y, calldataload(add(pubSignals, 352)))
                
                g1_mulAccC(_pVk, IC13x, IC13y, calldataload(add(pubSignals, 384)))
                
                g1_mulAccC(_pVk, IC14x, IC14y, calldataload(add(pubSignals, 416)))
                
                g1_mulAccC(_pVk, IC15x, IC15y, calldataload(add(pubSignals, 448)))
                
                g1_mulAccC(_pVk, IC16x, IC16y, calldataload(add(pubSignals, 480)))
                
                g1_mulAccC(_pVk, IC17x, IC17y, calldataload(add(pubSignals, 512)))
                
                g1_mulAccC(_pVk, IC18x, IC18y, calldataload(add(pubSignals, 544)))
                
                g1_mulAccC(_pVk, IC19x, IC19y, calldataload(add(pubSignals, 576)))
                
                g1_mulAccC(_pVk, IC20x, IC20y, calldataload(add(pubSignals, 608)))
                
                g1_mulAccC(_pVk, IC21x, IC21y, calldataload(add(pubSignals, 640)))
                
                g1_mulAccC(_pVk, IC22x, IC22y, calldataload(add(pubSignals, 672)))
                
                g1_mulAccC(_pVk, IC23x, IC23y, calldataload(add(pubSignals, 704)))
                
                g1_mulAccC(_pVk, IC24x, IC24y, calldataload(add(pubSignals, 736)))
                
                g1_mulAccC(_pVk, IC25x, IC25y, calldataload(add(pubSignals, 768)))
                
                g1_mulAccC(_pVk, IC26x, IC26y, calldataload(add(pubSignals, 800)))
                
                g1_mulAccC(_pVk, IC27x, IC27y, calldataload(add(pubSignals, 832)))
                
                g1_mulAccC(_pVk, IC28x, IC28y, calldataload(add(pubSignals, 864)))
                
                g1_mulAccC(_pVk, IC29x, IC29y, calldataload(add(pubSignals, 896)))
                
                g1_mulAccC(_pVk, IC30x, IC30y, calldataload(add(pubSignals, 928)))
                
                g1_mulAccC(_pVk, IC31x, IC31y, calldataload(add(pubSignals, 960)))
                
                g1_mulAccC(_pVk, IC32x, IC32y, calldataload(add(pubSignals, 992)))
                
                g1_mulAccC(_pVk, IC33x, IC33y, calldataload(add(pubSignals, 1024)))
                
                g1_mulAccC(_pVk, IC34x, IC34y, calldataload(add(pubSignals, 1056)))
                
                g1_mulAccC(_pVk, IC35x, IC35y, calldataload(add(pubSignals, 1088)))
                
                g1_mulAccC(_pVk, IC36x, IC36y, calldataload(add(pubSignals, 1120)))
                
                g1_mulAccC(_pVk, IC37x, IC37y, calldataload(add(pubSignals, 1152)))
                
                g1_mulAccC(_pVk, IC38x, IC38y, calldataload(add(pubSignals, 1184)))
                
                g1_mulAccC(_pVk, IC39x, IC39y, calldataload(add(pubSignals, 1216)))
                
                g1_mulAccC(_pVk, IC40x, IC40y, calldataload(add(pubSignals, 1248)))
                
                g1_mulAccC(_pVk, IC41x, IC41y, calldataload(add(pubSignals, 1280)))
                
                g1_mulAccC(_pVk, IC42x, IC42y, calldataload(add(pubSignals, 1312)))
                

                // -A
                mstore(_pPairing, calldataload(pA))
                mstore(add(_pPairing, 32), mod(sub(q, calldataload(add(pA, 32))), q))

                // B
                mstore(add(_pPairing, 64), calldataload(pB))
                mstore(add(_pPairing, 96), calldataload(add(pB, 32)))
                mstore(add(_pPairing, 128), calldataload(add(pB, 64)))
                mstore(add(_pPairing, 160), calldataload(add(pB, 96)))

                // alpha1
                mstore(add(_pPairing, 192), alphax)
                mstore(add(_pPairing, 224), alphay)

                // beta2
                mstore(add(_pPairing, 256), betax1)
                mstore(add(_pPairing, 288), betax2)
                mstore(add(_pPairing, 320), betay1)
                mstore(add(_pPairing, 352), betay2)

                // vk_x
                mstore(add(_pPairing, 384), mload(add(pMem, pVk)))
                mstore(add(_pPairing, 416), mload(add(pMem, add(pVk, 32))))


                // gamma2
                mstore(add(_pPairing, 448), gammax1)
                mstore(add(_pPairing, 480), gammax2)
                mstore(add(_pPairing, 512), gammay1)
                mstore(add(_pPairing, 544), gammay2)

                // C
                mstore(add(_pPairing, 576), calldataload(pC))
                mstore(add(_pPairing, 608), calldataload(add(pC, 32)))

                // delta2
                mstore(add(_pPairing, 640), deltax1)
                mstore(add(_pPairing, 672), deltax2)
                mstore(add(_pPairing, 704), deltay1)
                mstore(add(_pPairing, 736), deltay2)


                let success := staticcall(sub(gas(), 2000), 8, _pPairing, 768, _pPairing, 0x20)

                isOk := and(success, mload(_pPairing))
            }

            let pMem := mload(0x40)
            mstore(0x40, add(pMem, pLastMem))

            // Validate that all evaluations ∈ F
            
            checkField(calldataload(add(_pubSignals, 0)))
            
            checkField(calldataload(add(_pubSignals, 32)))
            
            checkField(calldataload(add(_pubSignals, 64)))
            
            checkField(calldataload(add(_pubSignals, 96)))
            
            checkField(calldataload(add(_pubSignals, 128)))
            
            checkField(calldataload(add(_pubSignals, 160)))
            
            checkField(calldataload(add(_pubSignals, 192)))
            
            checkField(calldataload(add(_pubSignals, 224)))
            
            checkField(calldataload(add(_pubSignals, 256)))
            
            checkField(calldataload(add(_pubSignals, 288)))
            
            checkField(calldataload(add(_pubSignals, 320)))
            
            checkField(calldataload(add(_pubSignals, 352)))
            
            checkField(calldataload(add(_pubSignals, 384)))
            
            checkField(calldataload(add(_pubSignals, 416)))
            
            checkField(calldataload(add(_pubSignals, 448)))
            
            checkField(calldataload(add(_pubSignals, 480)))
            
            checkField(calldataload(add(_pubSignals, 512)))
            
            checkField(calldataload(add(_pubSignals, 544)))
            
            checkField(calldataload(add(_pubSignals, 576)))
            
            checkField(calldataload(add(_pubSignals, 608)))
            
            checkField(calldataload(add(_pubSignals, 640)))
            
            checkField(calldataload(add(_pubSignals, 672)))
            
            checkField(calldataload(add(_pubSignals, 704)))
            
            checkField(calldataload(add(_pubSignals, 736)))
            
            checkField(calldataload(add(_pubSignals, 768)))
            
            checkField(calldataload(add(_pubSignals, 800)))
            
            checkField(calldataload(add(_pubSignals, 832)))
            
            checkField(calldataload(add(_pubSignals, 864)))
            
            checkField(calldataload(add(_pubSignals, 896)))
            
            checkField(calldataload(add(_pubSignals, 928)))
            
            checkField(calldataload(add(_pubSignals, 960)))
            
            checkField(calldataload(add(_pubSignals, 992)))
            
            checkField(calldataload(add(_pubSignals, 1024)))
            
            checkField(calldataload(add(_pubSignals, 1056)))
            
            checkField(calldataload(add(_pubSignals, 1088)))
            
            checkField(calldataload(add(_pubSignals, 1120)))
            
            checkField(calldataload(add(_pubSignals, 1152)))
            
            checkField(calldataload(add(_pubSignals, 1184)))
            
            checkField(calldataload(add(_pubSignals, 1216)))
            
            checkField(calldataload(add(_pubSignals, 1248)))
            
            checkField(calldataload(add(_pubSignals, 1280)))
            
            checkField(calldataload(add(_pubSignals, 1312)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
