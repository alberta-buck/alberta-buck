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

contract DepositFoldA1Verifier {
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
    uint256 constant deltax1 = 7387656371482771637597026207306832314712412954387750271785232551716392924168;
    uint256 constant deltax2 = 16652984065408500665783152374794578491765322656005411524070336692072119029787;
    uint256 constant deltay1 = 6433538810241932869282329899226243687646065381632844615575604072976675749406;
    uint256 constant deltay2 = 11778125538138342776755190510911065504922977531970247176775965108977545880292;

    
    uint256 constant IC0x = 16689441372276580239159398474465997058820514371052003149201783522108434905289;
    uint256 constant IC0y = 1321656865948303340564034109761270851024930778177304308028624737962795341563;
    
    uint256 constant IC1x = 5634618590519025395320858592749682653651259874899426866858803757002795317191;
    uint256 constant IC1y = 15172501181271038897746183585433843659619094223819507320519919144689615782691;
    
    uint256 constant IC2x = 12593159465348141016935511035454119745947572435833179962296110978055398186685;
    uint256 constant IC2y = 21568472984796166998867375880284647911587418142073155024360752318397798647072;
    
    uint256 constant IC3x = 16022752655303596345753430438467369953545356603935694436164712564780651233935;
    uint256 constant IC3y = 21701559353437380209722838970935723732644956301649649313068605889976887777790;
    
    uint256 constant IC4x = 15111806317765217339881960282754991221636374671612140585581611331532160070423;
    uint256 constant IC4y = 844790025584223149700067638681870556615991371074795602093052585201191247992;
    
    uint256 constant IC5x = 4459890528283401537866731013638906887928160604262753468683968148998667652659;
    uint256 constant IC5y = 1268264877050955566146445909119936979574781871293727037556332032976506528784;
    
    uint256 constant IC6x = 380404807552570870966534932648932406808918710330424705744229832586733424023;
    uint256 constant IC6y = 11002801574181541989253568561515344940755635467396874194882646359481428216278;
    
    uint256 constant IC7x = 9066663540376324386560257415667373497158408816575348889953504127770183423521;
    uint256 constant IC7y = 11426746415679617363637803647903655255489258054862503039404556266048561540895;
    
    uint256 constant IC8x = 6021163220587089834803968782049112887175588568735629898338246648040656027340;
    uint256 constant IC8y = 17796068040682320522431764708242618477419509311032059832677268364890995734357;
    
    uint256 constant IC9x = 12690073773184604942566721551416290774360240306746871144508932580861712435227;
    uint256 constant IC9y = 923300702035186186975008398264047808335352959966881993055476448596777526420;
    
    uint256 constant IC10x = 21462961794940374500129903031356551603403110507968224039550555939080360286738;
    uint256 constant IC10y = 15382101707803587245901523100920408300090524442247444729750388989748847124816;
    
    uint256 constant IC11x = 17940395640548879964623363149058597427405572299739879615223193995848249895842;
    uint256 constant IC11y = 9153655429414874257984906834014281097458134883777530341403403919437258094361;
    
    uint256 constant IC12x = 534095604783915068472517918235454117542236913957016177854827663354571055184;
    uint256 constant IC12y = 12291590990484455314155962645950136832887430744142044056448384996205183910528;
    
    uint256 constant IC13x = 5093264747001347582325287319896622767707772587032544828939863953284189843665;
    uint256 constant IC13y = 18956071470392386912885729362928374170412746845665298878515873141798442019046;
    
    uint256 constant IC14x = 9459349224397410750208422334937736119086138909848584951175830876794516102019;
    uint256 constant IC14y = 12490896191197496269797196329226782079630492790335639740114817878283055611361;
    
    uint256 constant IC15x = 790941837172645341756842894421480754733252935246417654838527471859036820118;
    uint256 constant IC15y = 4405067516821109901061812755641476793373971267373621405926344006439667786283;
    
    uint256 constant IC16x = 7849345517035678376062368224128374216199973139176920495586636673672550967402;
    uint256 constant IC16y = 18410613102547542243563548090616958721799173846645178059059811858741391245538;
    
    uint256 constant IC17x = 19459435951748657610152609383505647286415920349573161762868057640208625306784;
    uint256 constant IC17y = 150462593716639966499243016192655223291154147481382295195525022587563037149;
    
    uint256 constant IC18x = 8653809052990290426082015871145783301951996447763252060749711138596008345970;
    uint256 constant IC18y = 12389673880156164543577055622281382890240591782412320389030240233245217290559;
    
    uint256 constant IC19x = 13849316270926297052157743877064370563363739600145746291089098405223156209784;
    uint256 constant IC19y = 13903910328486443454534543230106933914610025213925340814890537839209160166965;
    
    uint256 constant IC20x = 976507138770934613409069077654037437445940662812398508915092357673556347991;
    uint256 constant IC20y = 21540375427569123633820661232787338789995048381282856685245341917629916199441;
    
    uint256 constant IC21x = 132080744228147257679432724083937145354519641937357433901363155740602371144;
    uint256 constant IC21y = 19140285982307042568561140052330954719749463839780713161256084436596565005732;
    
    uint256 constant IC22x = 5687534783506678477911187628888566056538446259052976942864768590608863403103;
    uint256 constant IC22y = 15238028633365501473563003777954400989763136353953309031557347741207235926987;
    
    uint256 constant IC23x = 20206441425067765206796336847022597284664297773455636168008920174251327109895;
    uint256 constant IC23y = 6082374293192893745246549860567226134782819073797623806627631588206276226318;
    
    uint256 constant IC24x = 16210045351267811668310243118396923518419770094609337998387028453893381492423;
    uint256 constant IC24y = 18155855819487760495484670103154027711536092668545163253001813898616226935398;
    
    uint256 constant IC25x = 16581365623293353993374535494876405823348413601164322902983314182574110038932;
    uint256 constant IC25y = 9744959919526686868658511224585920205413714477098253142318298989960720457573;
    
    uint256 constant IC26x = 18539789723477507843511847321390226386688133294901471306962508105199973309036;
    uint256 constant IC26y = 5115201398886515457237889276608208856686117221670834914114663723557490630565;
    
    uint256 constant IC27x = 2655588797565530318858564183219239670917677353952178538717828975702050136097;
    uint256 constant IC27y = 14879025447762248593431578683474787289333155630751024235964351435583105459504;
    
    uint256 constant IC28x = 9976419124114913207497212709657216251092521557195752148498506244746180988979;
    uint256 constant IC28y = 17341739130314521293361605534777432074131130840412178817557753986143730100593;
    
    uint256 constant IC29x = 12896679683281545999936701464587719666120573540831251610444998371001381467849;
    uint256 constant IC29y = 10893140771518868029752880659611387710790609292376791218738043934845520435596;
    
    uint256 constant IC30x = 9594887101913755441193960041457330417394773234825331383300222402966578042934;
    uint256 constant IC30y = 7344278187293909116019295336001967489647991206677361996994101935438010947757;
    
    uint256 constant IC31x = 17132220419671278278794295825588525322206056000262833578625896644648960769359;
    uint256 constant IC31y = 1426767282612496284417591542641139290952953375161474599673339839581163177738;
    
    uint256 constant IC32x = 12983080947296462791702687760796506720787924374930528201534394133759758529695;
    uint256 constant IC32y = 17321300480405920255737640891904482170962058020342943193108065350582154782846;
    
    uint256 constant IC33x = 17739261275314517163674198535149826355409498955212927745570368957261317611968;
    uint256 constant IC33y = 15589029653841652764284638164594781303375399805676503761979307847649215586119;
    
    uint256 constant IC34x = 13458606099946205817399214263029716259786020379854673886340796173958290364134;
    uint256 constant IC34y = 13657539933577863298568693504727166208868407629572267256608886369444704186714;
    
    uint256 constant IC35x = 3103267620435563623410424092894747103413413373799889359556037002343573775522;
    uint256 constant IC35y = 12538483955783959943795365314079454922945648157845493402853967452925199170245;
    
    uint256 constant IC36x = 1399073618326507701229162220645591619591886049772565426189515141332783922404;
    uint256 constant IC36y = 1395304133947648968005508156615507380266552929736870232088644352658647128858;
    
    uint256 constant IC37x = 7000582516169488078421289731367489900094146516622600850711738252111300275784;
    uint256 constant IC37y = 14298069074674262093989605485459636895806646097884778323350157384281937821871;
    
    uint256 constant IC38x = 17545998405341608778035893189197467299250608690581802055570855286358981204078;
    uint256 constant IC38y = 6384990162480261761420695902454417521571899456993446653156406406185990532908;
    
    uint256 constant IC39x = 10516815104249290258274403137941811224538789306525603070939951271146327267948;
    uint256 constant IC39y = 17355087273380491031789592038977037810673188638825695794163449120276218108581;
    
    uint256 constant IC40x = 11605300964579401195254786001916337460029590821550216321071854144510444984583;
    uint256 constant IC40y = 18022230016257269332569968981707552393445871041566452938391191547325524345882;
    
    uint256 constant IC41x = 17343745834619720541183052000332057020157742620515044696424422590894599513639;
    uint256 constant IC41y = 3628901855707542055271427475341794483858772844515477772595980674673983851988;
    
    uint256 constant IC42x = 10846662843881330539342511433447561986599179254398845478936269472271486355171;
    uint256 constant IC42y = 15435147501246729720488582482027353777041283102653925418452175360246855946310;
    
    uint256 constant IC43x = 680374416393095891457895764649212512838671011262562625102064821365423771506;
    uint256 constant IC43y = 12399210735310029039909089903904844791224014477303350956627209494473254025658;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[43] calldata _pubSignals) public returns (bool) {
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
                
                g1_mulAccC(_pVk, IC43x, IC43y, calldataload(add(pubSignals, 1344)))
                

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
            
            checkField(calldataload(add(_pubSignals, 1344)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
